from conductr_cli import conduct_main, host, sandbox_stop, sandbox_common
from conductr_cli.constants import DEFAULT_SCHEME, DEFAULT_PORT, DEFAULT_BASE_PATH, DEFAULT_API_VERSION
from conductr_cli.exceptions import BindAddressNotFoundError, BintrayUnreachableError, InstanceCountError, \
    SandboxImageNotFoundError, JavaCallError, JavaUnsupportedVendorError, JavaUnsupportedVersionError, \
    JavaVersionParseError
from conductr_cli.resolvers import bintray_resolver
from conductr_cli.resolvers.bintray_resolver import BINTRAY_DOWNLOAD_REALM, BINTRAY_LIGHTBEND_ORG, \
    BINTRAY_CONDUCTR_REPO
from conductr_cli.screen_utils import headline
from requests.exceptions import HTTPError, ConnectionError
from subprocess import CalledProcessError

import logging
import re
import os
import shutil
import subprocess


NR_OF_INSTANCE_EXPRESSION = '[0-9]+\\:[0-9]+'
BIND_TEST_PORT = 19991  # The port used for testing if an address can be bound.
CONDUCTR_AKKA_REMOTING_PORT = 9004  # The port used by ConductR's Akka remoting.
NR_OF_PROXY_INSTANCE = 1  # Only run 1 instance of ConductR HAProxy since there's only one HAProxy running per machine.
SUPPORTED_JVM_VENDOR = "java"  # Oracle JVM vendor is `java`.
SUPPORTED_JVM_VERSION = (1, 8)  # Supports JVM version 1.8 and above.


class SandboxRunResult:
    def __init__(self, core_pids, core_addrs, agent_pids, agent_addrs, nr_of_proxy_instances):
        self.core_pids = core_pids
        self.core_addrs = core_addrs
        self.agent_pids = agent_pids
        self.agent_addrs = agent_addrs
        self.nr_of_proxy_instances = nr_of_proxy_instances
        self.host = str(core_addrs[0])

    scheme = DEFAULT_SCHEME
    port = DEFAULT_PORT
    base_path = DEFAULT_BASE_PATH
    api_version = DEFAULT_API_VERSION

    def __eq__(self, other):
        return self.__dict__ == other.__dict__ if isinstance(other, self.__class__) else False


def run(args, features):
    """
    Starts the ConductR core and agent.

    :param args: args parsed from the input arguments
    :param features: list of features which are specified via -f switch.
                     This is only relevant for Docker based sandbox since the features decides what port to expose
    :return: SandboxRunResult
    """
    nr_of_core_instances, nr_of_agent_instances = instance_count(args.image_version, args.nr_of_containers)

    validate_jvm_support()

    sandbox_stop.stop(args)

    log = logging.getLogger(__name__)
    log.info(headline('Starting ConductR'))

    bind_addrs = find_bind_addrs(max(nr_of_core_instances, nr_of_agent_instances), args.addr_range)

    core_extracted_dir, agent_extracted_dir = obtain_sandbox_image(args.image_dir, args.image_version)

    core_addrs = bind_addrs[0:nr_of_core_instances]
    core_pids = start_core_instances(core_extracted_dir, core_addrs)

    agent_addrs = bind_addrs[0:nr_of_agent_instances]
    agent_pids = start_agent_instances(agent_extracted_dir, bind_addrs[0:nr_of_agent_instances])

    return SandboxRunResult(core_pids, core_addrs, agent_pids, agent_addrs, nr_of_proxy_instances=NR_OF_PROXY_INSTANCE)


def log_run_attempt(args, run_result, is_started, wait_timeout):
    """
    Logs the run attempt. This method will be called after the completion of run method and when all the features has been started.

    :param args: args parsed from the input arguments
    :param run_result: the result from calling sandbox_run_jvm.run() - instance of sandbox_run_jvm.SandboxRunResult
    :param is_started: sets to true if sandbox is started
    :param wait_timeout: the amount of timeout waiting for sandbox to be started
    :return:
    """
    log = logging.getLogger(__name__)
    if not args.no_wait:
        if is_started:
            log.info(headline('Summary'))
            log.info('ConductR has been started:')

            nr_instance_core = len(run_result.core_pids)
            plural_core = 's' if nr_instance_core > 1 else ''
            log.info('  core: {} instance{}'.format(nr_instance_core, plural_core))

            nr_instance_agent = len(run_result.agent_pids)
            plural_agents = 's' if nr_instance_agent > 1 else ''
            log.info('  agent: {} instance{}'.format(nr_instance_agent, plural_agents))

            log.info('Check current bundle status with:')
            log.info('  conduct info')
            conduct_main.run(['info', '--host', run_result.host], configure_logging=False)
        else:
            log.info(headline('Summary'))
            log.error('ConductR has not been started within {} seconds.'.format(wait_timeout))
            log.error('Set the env CONDUCTR_SANDBOX_WAIT_RETRY_INTERVAL to increase the wait timeout.')


def instance_count(image_version, instance_expression):
    """
    Parses the instance expressions into number of core and agent instances, i.e.

    The expression `2` translates to 2 core instances and 2 agent instances.
    The expression `2:3` translates to 2 core instances and 3 agent instances.

    :param image_version:
    :param instance_expression:
    :return: a tuple containing number of core instances and number of agent instances.
    """
    try:
        nr_of_instances = int(instance_expression)
        return nr_of_instances, nr_of_instances
    except ValueError:
        match = re.search(NR_OF_INSTANCE_EXPRESSION, instance_expression)
        if match:
            parts = instance_expression.split(':')
            nr_of_core_instances = int(parts[0])
            nr_of_agent_instances = int(parts[-1])
            return nr_of_core_instances, nr_of_agent_instances
        else:
            raise InstanceCountError(image_version,
                                     instance_expression,
                                     'Number of containers must be an integer or '
                                     'a valid instance expression, i.e. 2:3 '
                                     'which translates to 2 core instances and 3 agent instances')


def validate_jvm_support():
    """
    Validates for the presence of supported JVM (i.e. Oracle JVM 8), else raise an exception to fail the sandbox run.
    """
    try:
        raw_output = subprocess.getoutput('java -version')
        lines = raw_output.splitlines()
        if lines:
            first_line = lines[0]
            parts = first_line.split(' ')
            if len(parts) == 3:
                jvm_vendor = parts[0]

                if jvm_vendor == SUPPORTED_JVM_VENDOR:
                    jvm_version = parts[2].replace('"', '')
                    jvm_version_parts = jvm_version.split('.')
                    if len(jvm_version_parts) >= 2:
                        jvm_version_major = int(jvm_version_parts[0])
                        jvm_version_minor = int(jvm_version_parts[1])
                        jvm_version_tuple = (jvm_version_major, jvm_version_minor)

                        if jvm_version_tuple >= SUPPORTED_JVM_VERSION:
                            return
                        else:
                            raise JavaUnsupportedVersionError(jvm_version)
                else:
                    raise JavaUnsupportedVendorError(jvm_vendor)

        raise JavaVersionParseError(raw_output)
    except CalledProcessError:
        raise JavaCallError('Failure calling `java -version`')


def find_bind_addrs(nr_of_addrs, addr_range):
    """
    Finds for the presence of address which can be bound to the sandbox given an address range, i.e.
    - Let's say 3 address aliases is required.
    - The address range is 192.168.128.0/24

    These addresses requires setup using ifconfig as such (MacOS example):

    sudo ifconfig lo0 alias 192.168.128.1 255.255.255.0
    sudo ifconfig lo0 alias 192.168.128.2 255.255.255.0
    sudo ifconfig lo0 alias 192.168.128.3 255.255.255.0

    This command will check if 192.168.128.1, 192.168.128.2, and 192.168.128.3 can be bound. The check is done by
    binding a socket to each of these address using a test port.

    If the number of required address is not present, provide the commands so the end user is able to copy-paste and
    execute these commands.

    :param nr_of_addrs: number of address aliases required
    :param addr_range: the range of address which is available to core and agent to bind to.
                       The address is specified in the CIDR format, i.e. 192.168.128.0/24
    """
    addrs_to_bind = []
    addrs_unavailable = []
    for ip_addr in addr_range.hosts():
        if host.can_bind(ip_addr, BIND_TEST_PORT):
            addrs_to_bind.append(ip_addr)
        else:
            addrs_unavailable.append(ip_addr)

        if len(addrs_to_bind) >= nr_of_addrs:
            break

    if len(addrs_to_bind) < nr_of_addrs:
        nr_of_addr_setup = nr_of_addrs - len(addrs_to_bind)
        setup_instructions = host.addr_alias_setup_instructions(addrs_unavailable[0:nr_of_addr_setup],
                                                                addr_range.netmask)
        raise BindAddressNotFoundError(setup_instructions)
    else:
        return addrs_to_bind


def obtain_sandbox_image(image_dir, image_version):
    """
    Obtains the sandbox image.

    The sandbox image is the .tgz binary of ConductR core and agent which is available as a download from Bintray.

    First the local cache is interrogated for the presence of the .tgz binary.

    If the binary is not yet available within the local cache, then it will be downloaded from Bintray. If the binary
    is present within the local cache, they will be used instead.

    The core binary will be expanded into the `${image_dir}/core`. The directory `${image_dir}/core` will be emptied
    before the binary is expanded.

    Similarly, the agent binary will be expanded into the `${image_dir}/agent`. The directory `${image_dir}/agent` will
    be emptied before the binary is expanded.

    :param image_dir: the directory where ConductR core and agent binaries will be cached, also the base directory
                      containing the expanded ConductR core and agent binaries.
    :param image_version: the version of the sandbox to be downloaded.
    :return: the pair containing path to the expanded core directory and path to the expanded agent directory
    """
    def resolve_binaries():
        """
        Resolves ConductR binaries given the `${bintray_package_name}` and `${image_version}`.
        First, the core and agent binaries are resolved from the `${image_dir}` cache directory. If not available,
        the binaries are downloaded from Bintray.

        The artifacts are available under the following Bintray repo:

        https://bintray.com/lightbend/commercial-releases/`${bintray_package_name}`

        As part of the download:
        - A progress bar will be displayed.
        - The download will be saved into `${image_dir}/${filename}.tgz.tmp`.
        Once download is complete, this file will be moved to `${image_dir}/${filename}.tgz`.

        Once downloaded, the binaries are cached in `${image_dir}`.

        :return: tuple of (core_path, agent_path)
        """
        core_path = resolve_binary_from_cache('{}/conductr-{}.tgz'
                                              .format(image_dir, image_version))
        agent_path = resolve_binary_from_cache('{}/conductr-agent-{}.tgz'
                                               .format(image_dir, image_version))

        if (not core_path) or (not agent_path):
            try:
                bintray_username, bintray_password = bintray_resolver.load_bintray_credentials()
                bintray_auth = (BINTRAY_DOWNLOAD_REALM, bintray_username, bintray_password)
                if not core_path:
                    _, _, core_path = bintray_resolver.bintray_download(
                        image_dir, BINTRAY_LIGHTBEND_ORG, BINTRAY_CONDUCTR_REPO,
                        core_info['bintray_package_name'], bintray_auth, version=image_version)

                if not agent_path:
                    _, _, agent_path = bintray_resolver.bintray_download(
                        image_dir, BINTRAY_LIGHTBEND_ORG, BINTRAY_CONDUCTR_REPO,
                        agent_info['bintray_package_name'], bintray_auth, version=image_version)
            except ConnectionError:
                raise BintrayUnreachableError('Bintray is unreachable.')
            except HTTPError:
                raise SandboxImageNotFoundError(core_info['type'], image_version)

        return core_path, agent_path

    def resolve_binary_from_cache(binary_cache_path):
        """
        Checks for the presence of the ConductR binary in the cache directory.

        :param binary_cache_path the path of the cached ConductR universal binary.
        :return: If present, return the path to the binary file, else return None.
        """
        if os.path.exists(binary_cache_path):
            return binary_cache_path
        else:
            return None

    def extract_binary(path, conductr_info):
        """
        The binary will be expanded into the `${extraction_dir}`.
        The directory `${extraction_dir}` will be emptied before the binary is expanded.

        :param path: the path to the core binary to be expanded.
        :param conductr_info: the information of the ConductR universal binary
        :return: path to the directory containing expanded core binary.
        """
        log = logging.getLogger(__name__)
        extraction_dir = conductr_info['extraction_dir']
        if os.path.exists(extraction_dir):
            shutil.rmtree(extraction_dir)
        os.makedirs(extraction_dir, mode=0o700)
        log.info('Extracting ConductR {} to {}'.format(conductr_info['type'], extraction_dir))
        shutil.unpack_archive(path, extraction_dir)
        binary_basename = os.path.splitext(os.path.basename(path))[0]
        extraction_subdir = '{}/{}'.format(extraction_dir, binary_basename)
        for filename in os.listdir(extraction_subdir):
            shutil.move('{}/{}'.format(extraction_subdir, filename), '{}/{}'.format(extraction_dir, filename))
        os.rmdir(extraction_subdir)
        return extraction_dir

    core_info, agent_info = sandbox_common.resolve_conductr_info(image_dir)

    core_binary_path, agent_binary_path = resolve_binaries()

    core_extracted_dir = extract_binary(core_binary_path, core_info)
    agent_extracted_dir = extract_binary(agent_binary_path, agent_info)

    return core_extracted_dir, agent_extracted_dir


def start_core_instances(core_extracted_dir, bind_addrs):
    """
    Starts the ConductR core process.

    Each instance is allocated an address to be bound based on the address range. For example:
    - Given 3 required core instances
    - Given the address range input of 192.168.128.0/24
    - The instances will be allocated these addresses: 192.168.128.1, 192.168.128.2, 192.168.128.3

    :param core_extracted_dir: the directory containing the files expanded from core's binary .tgz
    :param bind_addrs: a list of addresses which the core instances will bind to.
                       If there are 3 instances of core required, there will be 3 addresses supplied.
    :return: the pids of the core instances.
    """
    log = logging.getLogger(__name__)
    pids = []
    for idx, bind_addr in enumerate(bind_addrs):
        commands = [
            '{}/bin/conductr'.format(core_extracted_dir),
            '-Dconductr.ip={}'.format(bind_addr)
        ]
        if idx > 0:
            commands.extend([
                '--seed',
                '{}:{}'.format(bind_addrs[0], CONDUCTR_AKKA_REMOTING_PORT)
            ])

        log.info('Starting ConductR core instance {} on {}..'.format(idx, bind_addr))
        pid = subprocess.Popen(commands,
                               cwd=core_extracted_dir,
                               start_new_session=True,
                               stdout=subprocess.DEVNULL,
                               stdin=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL).pid
        pids.append(pid)
    return pids


def start_agent_instances(agent_extracted_dir, bind_addrs):
    """
    Starts the ConductR agent process.

    Each instance is allocated an address to be bound based on the address range. For example:
    - Given 3 required agent instances
    - Given the address range input of 192.168.128.0/24
    - The instances will be allocated these addresses: 192.168.128.1, 192.168.128.2, 192.168.128.3

    :param agent_extracted_dir: the directory containing the files expanded from agent's binary .tgz
    :param bind_addrs: a list of addresses which the core instances will bind to.
                       If there are 3 instances of core required, there will be 3 addresses supplied.
    :return: the pids of the agent instances.
    """
    log = logging.getLogger(__name__)
    pids = []
    for idx, bind_addr in enumerate(bind_addrs):
        commands = [
            '{}/bin/conductr-agent'.format(agent_extracted_dir),
            '-Dconductr.agent.ip={}'.format(bind_addr),
            '--core-node',
            '{}:{}'.format(bind_addr, CONDUCTR_AKKA_REMOTING_PORT)
        ]
        log.info('Starting ConductR agent instance {} on {}..'.format(idx, bind_addr))
        pid = subprocess.Popen(commands,
                               cwd=agent_extracted_dir,
                               start_new_session=True,
                               stdout=subprocess.DEVNULL,
                               stdin=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL).pid
        pids.append(pid)
    return pids
