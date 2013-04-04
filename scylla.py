from __future__ import print_function

import argparse
import ctypes
import json
import multiprocessing
import os
import subprocess
import sys
import time


KILL_TIMEOUT_S = 3

MT_PROGRESS = 'progress'
MT_RESULT = 'result'


class ScyllaError(Exception):
    pass

COLOR_DEFAULT = 0x7
COLOR_RED = 0xC
COLOR_GREEN = 0xA
COLOR_YELLOW = 0xE
COLOR_PURPLE = 0xD

STDOUT_LOCK = multiprocessing.Lock()


def print_colored(message, color=COLOR_DEFAULT):
    STDOUT_LOCK.acquire()
    STD_OUTPUT_HANDLE = -11
    stdout = ctypes.windll.kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
    ctypes.windll.kernel32.SetConsoleTextAttribute(stdout, color)
    print(message)
    ctypes.windll.kernel32.SetConsoleTextAttribute(stdout, COLOR_DEFAULT)
    STDOUT_LOCK.release()


# http://stackoverflow.com/a/1305651/492141
class dict2obj(dict):
    def __init__(self, dict_):
        super(dict2obj, self).__init__(dict_)
        for key in self:
            item = self[key]
            if isinstance(item, list):
                for idx, it in enumerate(item):
                    if isinstance(it, dict):
                        item[idx] = dict2obj(it)
            elif isinstance(item, dict):
                self[key] = dict2obj(item)

    def __getattr__(self, key):
        return self[key]


def quote(string):
    return ('"' + string.strip('"') + '"') if ' ' in string else string


def watch_process(create_args, timeout, queue):
    with open(create_args['stdout'], 'w') as logfile:
        create_args['stdout'] = logfile
        process = subprocess.Popen(**create_args)
        for _ in range(1, timeout + 1):
            if process.poll() is None:
                time.sleep(1)
        if process.poll() is None:
            process.kill()
            process.wait()
    queue.put(process.returncode)


def run_command(command, timeout, cwd, environ, log):
    create_args = {
        'args': command,
        'shell': True,
        'cwd': cwd,
        'env': environ,
        'stderr': subprocess.STDOUT
    }

    if timeout < 0:
        with open(log, 'w') as logfile:
            create_args['stdout'] = logfile
            process = subprocess.Popen(**create_args)
            process.wait()
            retcode = process.returncode
    else:
        queue = multiprocessing.Queue()
        create_args['stdout'] = log
        compiler = os.path.splitext(os.path.basename(log))[0]
        watchdog = multiprocessing.Process(
            target=watch_process, args=(create_args, timeout, queue))
        watchdog.start()
        retcode = queue.get()
        if retcode:
            print_colored('[%s] Killed by watchdog' % compiler, COLOR_RED)

    return retcode


def scassert(condition, message):
    if not condition:
        raise ScyllaError(message)


def normalize_paths(paths):
    result = []
    for path in paths:
        scassert(os.path.isdir(path), 'Directory %s not found' % path)
        result.append(os.path.normpath(path).encode('utf-8'))
    return result


def builder_wrapper(builder, args, queue):
    try:
        builder(args, queue)
    except KeyboardInterrupt:
        pass


def cmake_builder(args, queue):
    args = dict2obj(args)

    build = os.path.join(args.root, args.build_directory, args.compiler)
    if not os.path.exists(build):
        os.makedirs(build)
    lib = os.path.join(args.root, 'lib', args.compiler)
    bin = os.path.join(args.root, 'bin', args.compiler)
    logs = os.path.join(args.root, args.build_directory, 'logs')
    if not os.path.exists(logs):
        os.makedirs(logs)

    commands = []
    cmake_command = [
        args.executable,
        '-G', args.generator,
        '-DLIBRARY_OUTPUT_DIRECTORY=%s' % lib,
        '-DARCHIVE_OUTPUT_DIRECTORY=%s' % lib,
        '-DRUNTIME_OUTPUT_DIRECTORY=%s' % bin,
        args.root
    ]

    class Command(object):
        def __init__(self, cmd, status, fatal=True, timeout=-1):
            self.cmd = cmd
            self.fatal = fatal
            self.timeout = timeout
            self.status = status

    if args.clean:
        commands.append(
            Command(args.make_command + ['clean'], 'Cleaning', fatal=False))
    commands.append(Command(cmake_command, 'Running CMake'))
    commands.append(Command(args.make_command, 'Building'))

    args.test_command[0] = os.path.join(bin, args.test_command[0])
    commands.append(
        Command(args.test_command, 'Testing', timeout=KILL_TIMEOUT_S))

    logpath = os.path.join(logs, args.compiler + '.log')
    success = True
    for i, command in enumerate(commands):
        queue.put({
            'id': args.compiler, 'type': MT_PROGRESS, 'step': i + 1,
            'steps_count': len(commands), 'data': command.status})

        shell_command = ' '.join(map(quote, command.cmd))
        if args.setenv:
            template = 'CALL %s || EXIT 1\n'
            batpath = os.path.join(build, 'setenv.bat')
            with open(batpath, 'w') as batfile:
                batfile.write('@ECHO OFF\n')
                for arg in args.setenv:
                    batfile.write(template % quote(arg))
                batfile.write(template % shell_command)
            shell_command = quote(batpath)

        retcode = run_command(
            shell_command, command.timeout,
            cwd=build, environ=args.environ, log=logpath)
        if retcode and command.fatal:
            success = False
            break

    queue.put({
        'id': args.compiler, 'type': MT_RESULT,
        'success': success, 'output': open(logpath).read()})


def main():
    def file_type(path):
        if not os.path.isfile(path):
            raise argparse.ArgumentTypeError('%s is not a file' % path)
        return path

    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--config', type=file_type, metavar='<path>',
        default=os.path.join(os.path.dirname(__file__), 'config.json'),
        help='path to compilers config file')
    parser.add_argument(
        'project', type=file_type, metavar='<path>',
        help='path to project config file')
    parser.add_argument(
        '--clean', action='store_true', help='perform clean build')
    parser.add_argument(
        '--verbose', action='store_true', help='perform clean build')
    parser.add_argument(
        '--fatal', action='store_true')
    args = parser.parse_args()

    with open(args.config) as config_file:
        try:
            config = dict2obj(json.load(config_file))
            config_project = dict2obj(json.load(open(args.project)))
        except ValueError:
            raise ScyllaError('Invalid config file')

    for project_name, project in config_project.iteritems():
        build_system = config.build_systems.get(project.build_system, None)
        scassert(
            build_system is not None, 'Unknown build system %s' % build_system)

        queue = multiprocessing.Queue()
        processes = []
        for compiler_name, compiler in config.compilers.iteritems():
            path = normalize_paths(build_system.path + compiler.path)
            environ = os.environ
            environ['PATH'] = os.pathsep.join(path) + environ['PATH']
            for name, value in compiler.environ.iteritems():
                environ[name] = value.encode('utf-8')

            build_args = compiler.build_systems[project.build_system]
            build_args['executable'] = build_system.executable
            build_args['compiler'] = compiler_name
            build_args['clean'] = args.clean
            build_args['build_directory'] = project.build_directory
            build_args['test_command'] = project.test_command
            build_args['root'] = os.getcwd()
            build_args['environ'] = environ
            build_args['setenv'] = compiler.setenv

            builder = globals()['%s_builder' % project.build_system]
            process = multiprocessing.Process(
                target=builder_wrapper,
                args=(builder, dict(build_args), queue))

            processes.append(process)
            process.start()

        def step2progress(steps_count, step):
            return int(step / float(steps_count) * 100)

        finished = 0
        stderrs = []
        while finished < len(processes):
            message = dict2obj(queue.get())
            if message.type == MT_PROGRESS:
                print_colored('[%s] [%d/%d] %s' % (
                    message.id, message.step, message.steps_count,
                    message.data))
            elif message.type == MT_RESULT:
                finished += 1
                if not message.success or args.verbose:
                    stderrs.append((message.id, message.output))
                if not message.success:
                    print_colored('[%s] FAILED' % message.id, COLOR_RED)
                    if args.fatal:
                        for process in processes:
                            process.terminate()
                            process.join()
                        break
                else:
                    print_colored('[%s] PASSED' % message.id, COLOR_GREEN)

        templates2colors = (
            ('error C', COLOR_RED),
            ('error:', COLOR_RED),
            ('FAILED', COLOR_RED),
            ('warning:', COLOR_YELLOW),
            ('', COLOR_DEFAULT)
        )

        for compiler, stderr in stderrs:
            print_colored('[%s] OUTPUT START' % compiler, COLOR_PURPLE)
            for line in stderr.splitlines():
                for template, color in templates2colors:
                    if template in line:
                        print_colored(line, color)
                        break
            print_colored('[%s] OUTPUT END' % compiler, COLOR_PURPLE)

        for process in processes:
            process.join()

    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except ScyllaError, ex:
        print_colored(ex.args[0])
    except KeyboardInterrupt:
        print_colored('Interrupted by user')
    sys.exit(1)
