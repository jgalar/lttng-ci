#!/usr/bin/python3
# Copyright (C) 2016 - Francis Deslauriers <francis.deslauriers@efficios.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import argparse
import json
import os
import random
import sys
import time
import xmlrpc.client
from urllib.parse import urljoin
from urllib.request import urlretrieve
import yaml
from jinja2 import Environment, FileSystemLoader

USERNAME = 'lava-jenkins'
HOSTNAME = 'lava-master-02.internal.efficios.com'
OBJSTORE_URL = "https://obj.internal.efficios.com/lava/results/"

class TestType():
    """ Enum like for test type """
    baremetal_benchmarks = 1
    baremetal_tests = 2
    kvm_tests = 3
    kvm_fuzzing_tests = 4
    values = {
        'baremetal-benchmarks' : baremetal_benchmarks,
        'baremetal-tests' : baremetal_tests,
        'kvm-tests' : kvm_tests,
        'kvm-fuzzing-tests' : kvm_fuzzing_tests,
    }

class DeviceType():
    """ Enum like for device type """
    x86 = 'x86'
    kvm = 'qemu'
    values = {
        'kvm' : kvm,
        'x86' : x86,
    }

def get_job_bundle_content(server, job):
    try:
        bundle_sha = server.scheduler.job_status(str(job))['bundle_sha1']
        bundle = server.dashboard.get(bundle_sha)
    except xmlrpc.client.Fault as error:
        print('Error while fetching results bundle', error.faultString)
        raise error

    return json.loads(bundle['content'])

def check_job_all_test_cases_state_count(server, job):
    """
    Parse the results bundle to see the run-tests testcase
    of the lttng-kernel-tests passed successfully
    """
    print("Testcase result:")
    content = server.results.get_testjob_results_yaml(str(job))
    testcases = yaml.load(content)

    passed_tests = 0
    failed_tests = 0
    for testcase in testcases:
        if testcase['result'] != 'pass':
            print("\tFAILED {}\n\t\t See http://{}{}".format(
                testcase['name'],
                HOSTNAME,
                testcase['url']
            ))
            failed_tests += 1
        else:
            passed_tests += 1
    return (passed_tests, failed_tests)

def fetch_benchmark_results(build_id):
    """
    Get the benchmark results from the objstore
    save them as CSV files localy
    """
    testcases = ['processed_results_close.csv',
                 'processed_results_ioctl.csv',
                 'processed_results_open_efault.csv',
                 'processed_results_open_enoent.csv',
                 'processed_results_dup_close.csv',
                 'processed_results_raw_syscall_getpid.csv',
                 'processed_results_lttng_test_filter.csv']
    for testcase in testcases:
        url = urljoin(OBJSTORE_URL, "{:s}/{:s}".format(build_id, testcase))
        print('Fetching {}'.format(url))
        urlretrieve(url, testcase)

def print_test_output(server, job):
    """
    Parse the attachment of the testcase to fetch the stdout of the test suite
    """
    job_finished, log = server.scheduler.jobs.logs(str(job))
    logs = yaml.load(log.data.decode('ascii'))
    print_line = False
    for line in logs:
        if line['lvl'] != 'target':
            continue
        if line['msg'] == '<LAVA_SIGNAL_STARTTC run-tests>':
            print('---- TEST SUITE OUTPUT BEGIN ----')
            print_line = True
            continue
        if line['msg'] == '<LAVA_SIGNAL_ENDTC run-tests>':
            print('----- TEST SUITE OUTPUT END -----')
            break
        if print_line:
            print("{} {}".format(line['dt'], line['msg']))

def get_vlttng_cmd(lttng_tools_commit, lttng_ust_commit=None):
    """
    Return vlttng cmd to be used in the job template for setup.
    """

    vlttng_cmd = 'vlttng --jobs=$(nproc) --profile urcu-master' \
                    ' --override projects.babeltrace.build-env.PYTHON=python3' \
                    ' --override projects.babeltrace.build-env.PYTHON_CONFIG=python3-config' \
                    ' --profile babeltrace-stable-1.4' \
                    ' --profile babeltrace-python' \
                    ' --profile lttng-tools-master' \
                    ' --override projects.lttng-tools.checkout='+lttng_tools_commit + \
                    ' --profile lttng-tools-no-man-pages'

    if lttng_ust_commit is not None:
        vlttng_cmd += ' --profile lttng-ust-master ' \
                    ' --override projects.lttng-ust.checkout='+lttng_ust_commit+ \
                    ' --profile lttng-ust-no-man-pages'

    vlttng_path = '/tmp/virtenv'

    vlttng_cmd += ' ' + vlttng_path

    return vlttng_cmd

def main():
    nfsrootfs = "https://obj.internal.efficios.com/lava/rootfs/rootfs_amd64_xenial_2018-12-05.tar.gz"
    test_type = None
    parser = argparse.ArgumentParser(description='Launch baremetal test using Lava')
    parser.add_argument('-t', '--type', required=True)
    parser.add_argument('-j', '--jobname', required=True)
    parser.add_argument('-k', '--kernel', required=True)
    parser.add_argument('-lm', '--lmodule', required=True)
    parser.add_argument('-tc', '--tools-commit', required=True)
    parser.add_argument('-id', '--build-id', required=True)
    parser.add_argument('-uc', '--ust-commit', required=False)
    parser.add_argument('-d', '--debug', required=False, action='store_true')
    args = parser.parse_args()

    if args.type not in TestType.values:
        print('argument -t/--type {} unrecognized.'.format(args.type))
        print('Possible values are:')
        for k in TestType.values:
            print('\t {}'.format(k))
        return -1

    lava_api_key = None
    if not args.debug:
        try:
            lava_api_key = os.environ['LAVA2_JENKINS_TOKEN']
        except Exception as error:
            print('LAVA2_JENKINS_TOKEN not found in the environment variable. Exiting...',
                  error)
            return -1

    jinja_loader = FileSystemLoader(os.path.dirname(os.path.realpath(__file__)))
    jinja_env = Environment(loader=jinja_loader, trim_blocks=True,
                            lstrip_blocks=True)
    jinja_template = jinja_env.get_template('template_lava_job.jinja2')

    test_type = TestType.values[args.type]

    if test_type in [TestType.baremetal_benchmarks, TestType.baremetal_tests]:
        device_type = DeviceType.x86
    else:
        device_type = DeviceType.kvm

    vlttng_path = '/tmp/virtenv'

    vlttng_cmd = get_vlttng_cmd(args.tools_commit, args.ust_commit)

    context = dict()
    context['DeviceType'] = DeviceType
    context['TestType'] = TestType

    context['job_name'] = args.jobname
    context['test_type'] = test_type
    context['random_seed'] = random.randint(0, 1000000)
    context['device_type'] = device_type

    context['vlttng_cmd'] = vlttng_cmd
    context['vlttng_path'] = vlttng_path

    context['kernel_url'] = args.kernel
    context['nfsrootfs_url'] = nfsrootfs
    context['lttng_modules_url'] = args.lmodule
    context['jenkins_build_id'] = args.build_id

    context['kprobe_round_nb'] = 10

    render = jinja_template.render(context)

    print('Job to be submitted:')

    print(render)

    if args.debug:
        return 0

    server = xmlrpc.client.ServerProxy('http://%s:%s@%s/RPC2' % (USERNAME, lava_api_key, HOSTNAME))

    for attempt in range(10):
        try:
            jobid = server.scheduler.submit_job(render)
        except xmlrpc.client.ProtocolError as error:
            print('Protocol error on submit, sleeping and retrying. Attempt #{}'
                  .format(attempt))
            time.sleep(5)
            continue
        else:
            break

    print('Lava jobid:{}'.format(jobid))
    print('Lava job URL: http://lava-master-02.internal.efficios.com/scheduler/job/{}'.format(jobid))

    #Check the status of the job every 30 seconds
    jobstatus = server.scheduler.job_state(jobid)['job_state']
    running = False
    while jobstatus in ['Submitted', 'Scheduling', 'Scheduled', 'Running']:
        if not running and jobstatus == 'Running':
            print('Job started running')
            running = True
        time.sleep(30)
        try:
            jobstatus = server.scheduler.job_state(jobid)['job_state']
        except xmlrpc.client.ProtocolError as error:
            print('Protocol error, retrying')
            continue
    print('Job ended with {} status.'.format(jobstatus))

    if jobstatus != 'Finished':
        return -1

    if test_type is TestType.kvm_tests or test_type is TestType.baremetal_tests:
        print_test_output(server, jobid)
    elif test_type is TestType.baremetal_benchmarks:
        fetch_benchmark_results(args.build_id)

    passed, failed = check_job_all_test_cases_state_count(server, jobid)
    print('With {} passed and {} failed Lava test cases.'.format(passed, failed))

    if failed != 0:
        return -1

    return 0

if __name__ == "__main__":
    sys.exit(main())
