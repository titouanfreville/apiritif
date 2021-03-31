"""

Copyright 2017 BlazeMeter Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
import copy
import unicodecsv as csv
import json
import logging
import multiprocessing
import os
import sys
import time
import traceback
import asyncio
import queue
from optparse import OptionParser
from asyncio import Task

from nose.config import Config, all_config_files
from nose.core import TestProgram
from nose.loader import defaultTestLoader
from nose.plugins import Plugin
from nose.plugins.manager import DefaultPluginManager

import apiritif
import apiritif.thread as thread
import apiritif.store as store
from apiritif.utils import NormalShutdown, log, get_trace


# TODO how to implement hits/s control/shape?
# TODO: VU ID for script
# TODO: disable assertions for load mode


class Params(object):
    def __init__(self):
        super(Params, self).__init__()
        self.worker_index = 0
        self.report = None

        self.delay = 0

        self.concurrency = 1
        self.iterations = 1
        self.ramp_up = 0
        self.steps = 0
        self.hold_for = 0

        self.verbose = False

        self.tests = None

    def __repr__(self):
        return "%s" % self.__dict__


class Supervisor(Task):
    """
    apiritif-loadgen CLI utility
        overwatch workers, kill them when terminated
        probably reports through stdout log the names of report files
    :type params: Params
    """

    def __init__(self, params):
        self.params = params
        self.workers = []

        if self.params.report.lower().endswith(".ldjson"):
            store.writer = LDJSONSampleWriter(self.params.report)
        else:
            store.writer = JTLSampleWriter(self.params.report)

        super(Supervisor, self).__init__(coro=self._start_workers())

    async def finish(self):
        log.info("Workers finished, awaiting result writer")
        await store.writer.finish()
        log.info("Results written, shutting down")

    def _get_worker_params(self):
        if not self.params.steps or self.params.steps < 0:
            self.params.steps = sys.maxsize

        step_granularity = self.params.ramp_up / self.params.steps
        for worker_index in range(self.params.concurrency):
            delay = worker_index * float(self.params.ramp_up) / self.params.concurrency
            delay -= delay % step_granularity if step_granularity else 0

            params = copy.deepcopy(self.params)
            params.worker_index = worker_index
            params.delay = delay
            yield params

    async def _start_workers(self):
        log.info("Total workers: %s", self.params.concurrency)

        thread.set_total(self.params.concurrency)
        args = list(self._get_worker_params())

        try:
            self.workers = [asyncio.create_task(self._spawn_worker(worker_args)) for worker_args in args]
            await asyncio.gather(*self.workers)
            # TODO: watch the total test duration, if set, 'cause iteration might last very long
        finally:
            await self.finish()

    async def _spawn_worker(self, params):
        """
        This method has to be module level function

        :type params: Params
        """
        setup_logging(params)
        log.info("Adding worker: idx=%s\tresults=%s", params.worker_index, params.report)
        await Worker(params)


class Worker(Task):
    def __init__(self, params):
        """
        :type params: Params
        """
        self.params = params
        self.futures = []
        super(Worker, self).__init__(self.run_nose())

    async def run_nose(self):
        """
        :type params: Params
        """
        thread.set_index(self.params.worker_index)
        log.debug("[%s] Starting nose iterations: %s", self.params.worker_index, self.params)
        assert isinstance(self.params.tests, list)
        # argv.extend(['--with-apiritif', '--nocapture', '--exe', '--nologcapture'])

        end_time = self.params.ramp_up + self.params.hold_for
        end_time += time.time() if end_time else 0
        await asyncio.sleep(self.params.delay)

        plugin = ApiritifPlugin()
        store.writer.concurrency += 1

        config = Config(env=os.environ, files=all_config_files(), plugins=DefaultPluginManager())
        config.plugins.addPlugins(extraplugins=[plugin])
        config.testNames = self.params.tests
        config.verbosity = 3 if self.params.verbose else 0
        if self.params.verbose:
            config.stream = open(os.devnull, "w")  # FIXME: use "with", allow writing to file/log

        iteration = 0
        try:
            while True:
                log.debug("Starting iteration:: index=%d,start_time=%.3f", iteration, time.time())
                thread.set_iteration(iteration)
                ApiritifTestProgram(config=config)
                log.debug("Finishing iteration:: index=%d,end_time=%.3f", iteration, time.time())

                iteration += 1

                # reasons to stop
                if plugin.stop_reason:
                    log.debug("[%s] finished prematurely: %s", self.params.worker_index, plugin.stop_reason)
                elif 0 < self.params.iterations <= iteration:
                    log.debug("[%s] iteration limit reached: %s", self.params.worker_index, self.params.iterations)
                elif 0 < end_time <= time.time():
                    log.debug("[%s] duration limit reached: %s", self.params.worker_index, self.params.hold_for)
                else:
                    continue  # continue if no one is faced

                break
        finally:
            store.writer.concurrency -= 1

            if self.params.verbose:
                config.stream.close()

    def __reduce__(self):
        raise NotImplementedError()


class ApiritifTestProgram(TestProgram):
    def __init__(self, *args, **kwargs):
        super(ApiritifTestProgram, self).__init__(*args, **kwargs)
        self.testNames = None

    def parseArgs(self, argv):
        self.exit = False
        self.testNames = self.config.testNames
        self.testLoader = defaultTestLoader(config=self.config)
        self.createTests()


class LDJSONSampleWriter(Task):
    """
    :type out_stream: file
    """

    def __init__(self, output_file):
        self.concurrency = 0
        self.output_file = output_file
        self.out_stream = None
        self._samples_queue = queue.Queue()
        self._writing = True

        self._init_out_stream()

        super(LDJSONSampleWriter, self).__init__(self._writer())

    def is_alive(self):
        return not self.done()

    async def finish(self):
        self._writing = False
        await self
        self.out_stream.close()

    def add(self, sample, test_count, success_count):
        self._samples_queue.put_nowait((sample, test_count, success_count))

    def is_queue_empty(self):
        return self._samples_queue.empty()

    def _init_out_stream(self):
        self.out_stream = open(self.output_file, "wb")

    async def _writer(self):
        while self._writing:
            await asyncio.sleep(0.1)

            while not self._samples_queue.empty():
                item = self._samples_queue.get(block=True)
                try:
                    sample, test_count, success_count = item
                    self._write_sample(sample, test_count, success_count)
                except BaseException as exc:
                    log.debug("Processing sample failed: %s\n%s", str(exc), traceback.format_exc())
                    log.warning("Couldn't process sample, skipping")
                    continue

    def _write_sample(self, sample, test_count, success_count):
        line = json.dumps(sample.to_dict()) + "\n"
        self.out_stream.write(line.encode('utf-8'))
        self.out_stream.flush()

        report_pattern = "%s,Total:%d Passed:%d Failed:%d\n"
        failed_count = test_count - success_count
        sys.stdout.write(report_pattern % (sample.test_case, test_count, success_count, failed_count))
        sys.stdout.flush()


class JTLSampleWriter(LDJSONSampleWriter):
    def __init__(self, output_file):
        super(JTLSampleWriter, self).__init__(output_file)

    def _init_out_stream(self):
        super(JTLSampleWriter, self)._init_out_stream()

        fieldnames = ["timeStamp", "elapsed", "Latency", "label", "responseCode", "responseMessage", "success",
                      "allThreads", "bytes"]
        endline = '\n'  # \r will be preprended automatically because out_stream is opened in text mode
        self.writer = csv.DictWriter(self.out_stream, fieldnames=fieldnames, dialect=csv.excel, lineterminator=endline,
                                     encoding='utf-8')
        self.writer.writeheader()
        self.out_stream.flush()

    def _write_sample(self, sample, test_count, success_count):
        """
        :type sample: Sample
        :type test_count: int
        :type success_count: int
        """
        self._write_request_subsamples(sample)

    def _get_sample_type(self, sample):
        if sample.path:
            last = sample.path[-1]
            return last.type
        else:
            return None

    def _write_request_subsamples(self, sample):
        if self._get_sample_type(sample) == "request":
            self._write_single_sample(sample)
        elif sample.subsamples:
            for sub in sample.subsamples:
                self._write_request_subsamples(sub)
        else:
            self._write_single_sample(sample)

    def _write_single_sample(self, sample):
        """
        :type sample: Sample
        """
        bytes = sample.extras.get("responseHeadersSize", 0) + 2 + sample.extras.get("responseBodySize", 0)

        message = sample.error_msg
        if not message:
            message = sample.extras.get("responseMessage")
        if not message:
            for sample in sample.subsamples:
                if sample.error_msg:
                    message = sample.error_msg
                    break
                elif sample.extras.get("responseMessage"):
                    message = sample.extras.get("responseMessage")
                    break
        self.writer.writerow({
            "timeStamp": int(1000 * sample.start_time),
            "elapsed": int(1000 * sample.duration),
            "Latency": 0,  # TODO
            "label": sample.test_case,

            "bytes": bytes,

            "responseCode": sample.extras.get("responseCode"),
            "responseMessage": message,
            "allThreads": self.concurrency,  # TODO: there will be a problem aggregating concurrency for rare samples
            "success": "true" if sample.status == "PASSED" else "false",
        })
        self.out_stream.flush()


# noinspection PyPep8Naming
class ApiritifPlugin(Plugin):
    """
    Saves test results in a format suitable for Taurus.
    :type sample_writer: LDJSONSampleWriter
    """

    name = 'apiritif'
    enabled = False

    def __init__(self):
        super(ApiritifPlugin, self).__init__()
        self.controller = store.SampleController(log)
        apiritif.put_into_thread_store(controller=self.controller)  # parcel for smart_transactions
        self.stop_reason = ""

    def finalize(self, result):
        """
        After all tests
        """
        if not self.controller.test_count:
            raise RuntimeError("Nothing to test.")

    def beforeTest(self, test):
        """
        before test run
        """
        thread.clean_transaction_handlers()
        thread.clean_logging_handlers()
        addr = test.address()  # file path, package.subpackage.module, class.method
        test_file, module_fqn, class_method = addr
        test_fqn = test.id()  # [package].module.class.method
        suite_name, case_name = test_fqn.split('.')[-2:]
        log.debug("Addr: %r", addr)
        log.debug("id: %r", test_fqn)

        if class_method is None:
            class_method = case_name

        description = test.shortDescription()
        self.controller.test_info = {
            "test_case": case_name,
            "suite_name": suite_name,
            "test_file": test_file,
            "test_fqn": test_fqn,
            "description": description,
            "module_fqn": module_fqn,
            "class_method": class_method}
        self.controller.beforeTest()  # create template of current_sample

    def startTest(self, test):
        self.controller.startTest()

    def stopTest(self, test):
        self.controller.stopTest()

    def afterTest(self, test):
        self.controller.afterTest()

    def addError(self, test, error):
        """
        when a test raises an uncaught exception
        :param test:
        :param error:
        :return:
        """
        # test_dict will be None if startTest wasn't called (i.e. exception in setUp/setUpClass)
        # status=BROKEN
        assertion_name = error[0].__name__
        error_msg = str(error[1]).split('\n')[0]
        error_trace = get_trace(error)
        if self.controller.current_sample is not None:
            self.controller.addError(assertion_name, error_msg, error_trace)
        else:  # error in test infrastructure (e.g. module setup())
            log.error("\n".join((assertion_name, error_msg, error_trace)))

    @staticmethod
    def isNormalShutdown(cls):
        cls_full_name = ".".join((cls.__module__, cls.__name__))
        ns_full_name = ".".join((NormalShutdown.__module__, NormalShutdown.__name__))
        return cls_full_name == ns_full_name

    def handleError(self, test, error):
        if self.isNormalShutdown(error[0]):
            self.add_stop_reason(error[1].args[0])  # remember it for run_nose() cycle
            return True
        else:
            return False

    def add_stop_reason(self, msg):
        if self.stop_reason:
            self.stop_reason += "\n"

        self.stop_reason += msg

    def addFailure(self, test, error):
        """
        when a test fails
        :param test:
        :param error:

        :return:
        """
        # status=FAILED
        self.controller.addFailure(error)

    def addSuccess(self, test):
        """
        when a test passes
        :param test:
        :return:
        """
        self.controller.addSuccess()


def cmdline_to_params():
    parser = OptionParser()
    parser.add_option('', '--concurrency', action='store', type="int", default=1)
    parser.add_option('', '--iterations', action='store', type="int", default=sys.maxsize)
    parser.add_option('', '--ramp-up', action='store', type="float", default=0)
    parser.add_option('', '--steps', action='store', type="int", default=sys.maxsize)
    parser.add_option('', '--hold-for', action='store', type="float", default=0)
    parser.add_option('', '--result-file-template', action='store', type="str", default="result.csv")
    parser.add_option('', '--verbose', action='store_true', default=False)
    opts, args = parser.parse_args()
    log.debug("%s %s", opts, args)

    params = Params()
    params.concurrency = opts.concurrency
    params.ramp_up = opts.ramp_up
    params.steps = opts.steps
    params.iterations = opts.iterations
    params.hold_for = opts.hold_for

    params.report = opts.result_file_template
    params.tests = args
    params.verbose = opts.verbose

    return params


def setup_logging(params):
    logformat = "%(asctime)s:%(levelname)s:%(process)s:%(thread)s:%(name)s:%(message)s"
    apiritif.http.log.setLevel(logging.WARNING)
    if params.verbose:
        logging.basicConfig(level=logging.DEBUG, stream=sys.stdout, format=logformat)
    else:
        logging.basicConfig(level=logging.INFO, stream=sys.stdout, format=logformat)
    log.setLevel(logging.INFO)  # TODO: do we need to include apiritif debug logs in verbose mode?


def main():
    cmd_params = cmdline_to_params()
    setup_logging(cmd_params)

    loop = asyncio.get_event_loop()
    loop.run_until_complete(Supervisor(cmd_params))


if __name__ == '__main__':
    main()
