# -*- coding: utf-8 -*-
from __future__ import absolute_import, print_function

from .utils.visibility import getLogger
logger = getLogger(__name__)

import logging
plainLogger = logging.getLogger(__name__)

import getpass
import gzip
import hashlib
import io
import os
import re
import ruamel
import shutil
import subprocess
import tarfile
import time
import tempfile

import requests
from six.moves.urllib.parse import urljoin

from .exceptions import AnsibleContainerAlreadyInitializedException,\
                        AnsibleContainerRegistryAttributeException, \
                        AnsibleContainerException
from .utils import *
from . import __version__, host_only, conductor_only
from container.utils.loader import load_engine

REMOVE_HTTP = re.compile('^https?://')
DEFAULT_CONDUCTOR_BASE = 'centos:7'
DEFAULT_DEPLOYMENT_PATH = '~/ansible-deployment/'


@host_only
def hostcmd_init(base_path, project=None, **kwargs):
    if project:
        if os.listdir(base_path):
            raise AnsibleContainerAlreadyInitializedException(
                u'The init command can only be run in an empty directory.')
        try:
            namespace, name = project.split('.', 1)
        except ValueError:
            raise ValueError(u'Invalid project name: %r; use '
                             u'"username.project" style syntax.' % project)
        galaxy_base_url = kwargs.pop('server')
        response = requests.get(urljoin(galaxy_base_url, '/api/v1/roles/'),
                                params={'role_type': 'APP',
                                        'namespace': namespace,
                                        'name': name},
                                headers={'Accepts': 'application/json'})
        try:
            response.raise_for_status()
        except requests.RequestException as e:
            raise requests.RequestException(u'Could not find %r on Galaxy '
                                            u'server %r: %r' % (project,
                                                                galaxy_base_url,
                                                                e))
        if not response.json()['count']:
            raise ValueError(u'Could not find %r on Galaxy '
                             u'server %r: No such container app' % (project,
                                                                    galaxy_base_url))
        container_app_data = response.json()['results'][0]
        if not all([container_app_data[k] for k in ['github_user',
                                                    'github_repo']]):
            raise ValueError(u'Container app %r does not have a GitHub URL' % project)
        archive_url = u'https://github.com/%s/%s/archive/%s.tar.gz' % (
            container_app_data['github_user'],
            container_app_data['github_repo'],
            container_app_data['github_branch'] or u'master'
        )
        archive = requests.get(archive_url)
        try:
            archive.raise_for_status()
        except requests.RequestException as e:
            raise requests.RequestException(u'Could not get archive at '
                                            u'%r: %r' % (archive_url, e))
        faux_file = io.BytesIO(archive.content)
        gz_obj = gzip.GzipFile(fileobj=faux_file)
        tar_obj = tarfile.TarFile(fileobj=gz_obj)
        members = tar_obj.getmembers()
        # now we do the actual extraction to the path
        for member in members:
            # we only extract files, and remove any relative path
            # bits that might be in the file for security purposes
            # and drop the leading directory, as mentioned above
            if member.isreg() or member.issym():
                parts = member.name.split(os.sep)[1:]
                final_parts = []
                for part in parts:
                    if part != '..' and '~' not in part and '$' not in part:
                        final_parts.append(part)
                member.name = os.path.join(*final_parts)
                tar_obj.extract(member, base_path)
        logger.info(u'Ansible Container initialized from Galaxy container app %r', project)
    else:
        container_cfg = os.path.join(base_path, 'container.yml')
        if os.path.exists(container_cfg):
            raise AnsibleContainerAlreadyInitializedException()
        if not os.path.exists(base_path):
            os.mkdir(base_path)
        template_dir = os.path.join(jinja_template_path(), 'init')
        context = {
            u'ansible_container_version': __version__,
            u'project_name': kwargs.get('project_name',
                                        os.path.basename(base_path)),
            u'default_base': DEFAULT_CONDUCTOR_BASE,
            u'deployment_output_path': DEFAULT_DEPLOYMENT_PATH
        }
        for tmpl_filename in os.listdir(template_dir):
            jinja_render_to_temp(template_dir,
                                 os.path.join('init', tmpl_filename),
                                 base_path,
                                 tmpl_filename.replace('.j2', ''),
                                 **context)
        logger.info('Ansible Container initialized.')


@host_only
def hostcmd_build(base_path, project_name, engine_name, var_file=None,
                 **kwargs):
    config = get_config(base_path, var_file=var_file)
    engine_obj = load_engine(['BUILD', 'RUN'],
                             engine_name, project_name or os.path.basename(base_path),
                             config['services'], **kwargs)

    conductor_container_id = engine_obj.get_container_id_for_service('conductor')
    if engine_obj.service_is_running('conductor'):
        engine_obj.stop_container(conductor_container_id, forcefully=True)

    if conductor_container_id is None or not kwargs.get('devel'):
        #TODO once we get a conductor running, figure out how to know it's running
        if engine_obj.CAP_BUILD_CONDUCTOR:
            engine_obj.build_conductor_image(
                base_path,
                (config['settings'] or {}).get('conductor_base', DEFAULT_CONDUCTOR_BASE),
                cache=kwargs['cache']
            )
        else:
            logger.warning(u'%s does not support building the Conductor image.',
                           engine_obj.display_name, engine=engine_obj.display_name)

    if conductor_container_id:
        engine_obj.delete_container(conductor_container_id)

    logger.debug('Config settings', config=config, rawsettings=config.get('settings'),
                 tconf=type(config), settings=config.get('settings', {}))
    engine_obj.await_conductor_command(
        'build', dict(config), base_path, kwargs,
        save_container=(config['settings'] or {}).get('save_build_container', False))


@host_only
def hostcmd_run(base_path, project_name, engine_name, var_file=None, cache=True,
                **kwargs):
    logger.debug('Got extra args to `run` command', arguments=kwargs)
    assert_initialized(base_path)
    config = get_config(base_path, var_file=var_file)
    logger.debug('Load engine: {}'.format(engine_name))
    engine_obj = load_engine(['RUN'],
                             engine_name, project_name or os.path.basename(base_path),
                             config['services'], **kwargs)
    if not engine_obj.CAP_RUN:
        msg = u'{} does not support the `run` command'.format(engine_obj.display_name)
        logger.error(msg, engine=engine_obj.display_name)
        raise Exception(msg)

    deployment_path = (config['settings'] or {}).get('deployment_output_path', DEFAULT_DEPLOYMENT_PATH)
    deployment_path = os.path.normpath(os.path.expanduser(deployment_path))
    params = {'deployment_output_path': deployment_path}
    if kwargs:
        params.update(kwargs)

    engine_obj.await_conductor_command(
        'run', dict(config), base_path, params,
        save_container=config.get('settings', {}).get('save_build_container', False))


@host_only
def hostcmd_destroy(base_path, project_name, engine_name, var_file=None, cache=True,
                  **kwargs):
    logger.debug('Got extra args to `destroy` command', arguments=kwargs)
    config = get_config(base_path, var_file=var_file)
    assert_initialized(base_path)

    engine_obj = load_engine(['RUN'],
                             engine_name, project_name or os.path.basename(base_path),
                             config['services'], **kwargs)
    if not engine_obj.CAP_RUN:
        msg = u'{} does not support this command.'.format(
            engine_obj.display_name)
        logger.error(msg, engine=engine_obj.display_name)
        raise Exception(msg)

    engine_obj.await_conductor_command(
        'destroy', dict(config), base_path, kwargs,
        save_container=config.get('save_build_container', False))

@host_only
def hostcmd_stop(base_path, project_name, engine_name, force=False, services=[],
                 **kwargs):
    config = get_config(base_path)
    engine_obj = load_engine(['RUN'],
                             engine_name, project_name or os.path.basename(base_path),
                             config['services'], **kwargs)

    engine_obj.await_conductor_command(
        'stop', dict(config), base_path, kwargs,
        save_container=config.get('settings', {}).get('save_build_container', False))


@host_only
def hostcmd_restart(base_path, project_name, engine_name, force=False, services=[],
                    **kwargs):
    config = get_config(base_path)
    engine_obj = load_engine(['RUN'],
                             engine_name, project_name or os.path.basename(base_path),
                             config['services'], **kwargs)

    engine_obj.await_conductor_command(
        'restart', dict(config), base_path, kwargs,
        save_container=config.get('settings', {}).get('save_build_container', False))


@host_only
def hostcmd_push(base_path, project_name, engine_name, var_file=None, **kwargs):
    """
    Push images to a registry. Requires authenticating with the registry prior to starting
    the push. If your engine's config file does not already contain an authorization for the
    registry, pass username and/or password. If you exclude password, you will be prompted.
    """
    assert_initialized(base_path)
    config = get_config(base_path, var_file=var_file)

    engine_obj = load_engine(['LOGIN', 'PUSH'],
                             engine_name, project_name or os.path.basename(base_path),
                             config['services'], **kwargs)

    config_path = kwargs.get('config_path', engine_obj.auth_config_path)
    username = kwargs.get('username')
    password = kwargs.get('password')
    push_to = kwargs.get('push_to')

    url = engine_obj.default_registry_url
    registry_name = engine_obj.default_registry_name
    namespace = None
    if push_to:
        if config.get('registries', dict()).get(push_to):
            url = config['registries'][push_to].get('url')
            namespace = config['registries'][push_to].get('namespace')
            if not url:
                raise AnsibleContainerRegistryAttributeException(
                    u"Registry {} missing required attribute 'url'".format(push_to)
                )
        else:
            url, namespace = resolve_push_to(push_to, engine_obj.default_registry_url)

    if username and not password:
        # If a username was supplied without a password, prompt for it
        while not password:
            password = getpass.getpass(u"Enter password for {0} at {1}: ".format(username, registry_name))

    if config_path:
        # Make sure the config_path exists
        #  - gives us a chance to create the file with correct permissions, if it does not exists
        #  - makes sure we mount a path to the conductor for a specific file
        config_path = os.path.normpath(os.path.expanduser(config_path))
        if os.path.exists(config_path) and os.path.isdir(config_path):
            raise AnsibleContainerException(
                u"Expecting --config-path to be a path to a file and not a directory"
            )
        elif not os.path.exists(config_path):
            # Make sure the directory path exists
            try:
                os.makedirs(os.path.dirname(config_path), 0o750)
            except OSError:
                raise AnsibleContainerException(
                    u"Failed to create the requested the path {}".format(os.path.dirname(config_path))
                )
            # Touch the file
            open(config_path, 'w').close()

    # If you ran build with --save-build-container, then you're broken without first removing
    #  the old build container.
    remove_existing_container(engine_obj, 'conductor')

    push_params = {}
    push_params.update(kwargs)
    push_params['config_path'] = config_path
    push_params['password'] = password
    push_params['url'] = url
    push_params['namespace'] = namespace

    conductor_container_id = engine_obj.run_conductor('push', dict(config), base_path, push_params)

    try:
        while engine_obj.service_is_running('conductor'):
            time.sleep(0.1)
    finally:
        if not config.get('settings', {}).get('save_build_container', False):
            logger.info('Conductor terminated. Cleaning up.')
            if engine_obj.service_is_running('conductor'):
                engine_obj.stop_container(conductor_container_id, forcefully=True)
            engine_obj.delete_container(conductor_container_id)
        else:
            logger.info('Conductor terminated. Preserving as requested.')


# def hostcmd_shipit(base_path, engine_name, pull_from=None, **kwargs):
#     assert_initialized(base_path)
#     engine_args = kwargs.copy()
#     engine_args.update(locals())
#     engine_obj = load_engine(**engine_args)
#     shipit_engine_name = kwargs.pop('shipit_engine')
#     project_name = os.path.basename(base_path).lower()
#     local_images = kwargs.get('local_images')
#
#     # determine the registry url and namespace the cluster will use to pull images
#     config = engine_obj.config
#     url = None
#     namespace = None
#     if not local_images:
#         if not pull_from:
#             url = engine_obj.default_registry_url
#         elif config.get('registries', {}).get(pull_from):
#             url = config['registries'][pull_from].get('url')
#             namespace = config['registries'][pull_from].get('namespace')
#             if not url:
#                 raise AnsibleContainerRegistryAttributeException("Registry %s missing required attribute 'url'."
#                                                                  % pull_from)
#             pull_from = None  # pull_from is now resolved to a url/namespace
#         if url and not namespace:
#             # try to get the username for the url from the container engine
#             try:
#                 namespace = engine_obj.registry_login(url=url)
#             except Exception as exc:
#                 if "Error while fetching server API version" in str(exc):
#                     msg = "Cannot connect to the Docker daemon. Is the daemon running?"
#                 else:
#                     msg = "Unable to determine namespace for registry %s. Error: %s. Either authenticate with the " \
#                           "registry or provide a namespace for the registry in container.yml" % (url, str(exc))
#                 raise AnsibleContainerRegistryAttributeException(msg)
#
#     config = engine_obj.get_config_for_shipit(pull_from=pull_from, url=url, namespace=namespace)
#
#     shipit_engine_obj = load_shipit_engine(AVAILABLE_SHIPIT_ENGINES[shipit_engine_name]['cls'],
#                                            config=config,
#                                            base_path=base_path,
#                                            project_name=project_name)
#
#     # create the role and sample playbook
#     shipit_engine_obj.run()
#     logger.info('Role %s created.' % project_name)
#
#     if kwargs.get('save_config'):
#         # generate and save the configuration templates
#         config_path = shipit_engine_obj.save_config()
#         logger.info('Saved configuration to %s' % config_path)


@host_only
def hostcmd_install(base_path, engine_name, roles=[], **kwargs):
    # FIXME: Refactor for Mk.II
    # assert_initialized(base_path)
    # engine_args = kwargs.copy()
    # engine_args.update(locals())
    # engine_obj = load_engine(**engine_args)
    #
    # with make_temp_dir() as temp_dir:
    #     engine_obj.orchestrate('install', temp_dir)
    pass


@host_only
def hostcmd_version(base_path, engine_name, debug=False, **kwargs):
    # FIXME: Refactor for Mk.II
    # print('Ansible Container, version', __version__)
    # if debug:
    #     print(u', '.join(os.uname()))
    #     print(sys.version, sys.executable)
    #     assert_initialized(base_path)
    #     engine_args = kwargs.copy()
    #     engine_args.update(locals())
    #     engine_obj = load_engine(**engine_args)
    #     engine_obj.print_version_info()
    pass


@host_only
def hostcmd_import(base_path, project_name, engine_name, **kwargs):
    engine_obj = load_engine(['IMPORT'],
                             engine_name,
                             project_name or os.path.basename(base_path),
                             {}, **kwargs)

    engine_obj.import_project(base_path, **kwargs)
    logger.info('Project imported.')


@host_only
def remove_existing_container(engine_obj, service_name):
    """
    Remove a container for an existing service. Handy for removing an existing conductor.
    """
    conductor_container_id = engine_obj.get_container_id_for_service(service_name)
    if engine_obj.service_is_running(service_name):
        engine_obj.stop_container(conductor_container_id, forcefully=True)
    if conductor_container_id:
        engine_obj.delete_container(conductor_container_id)


@host_only
def resolve_push_to(push_to, default_url):
    '''
    Given a push-to value, return the registry and namespace.

    :param push_to: string: User supplied --push-to value.
    :param default_url: string: Container engine's default_index value (e.g. docker.io).
    :return: tuple: registry_url, namespace
    '''
    protocol = 'http://' if push_to.startswith('http://') else 'https://'
    url = push_to = REMOVE_HTTP.sub('', push_to)
    namespace = None
    parts = url.split('/', 1)
    special_set = {'.', ':'}
    char_set = set([c for c in parts[0]])

    if len(parts) == 1:
        if not special_set.intersection(char_set) and parts[0] != 'localhost':
            registry_url = default_url
            namespace = push_to
        else:
            registry_url = protocol + parts[0]
    else:
        registry_url = protocol + parts[0]
        namespace = parts[1]

    return registry_url, namespace


@conductor_only
def run_playbook(playbook, engine, service_map, ansible_options='',
                 python_interpreter=None, debug=False, deployment_output_path=None, **kwargs):
    try:
        if deployment_output_path:
            remove_tmpdir = False
            output_dir = deployment_output_path
        else:
            remove_tmpdir = True
            output_dir = tempfile.mkdtemp()
        playbook_path = os.path.join(output_dir, 'playbook.yml')
        logger.debug("writing playbook to {}".format(playbook_path))
        with open(playbook_path, 'w') as ofs:
            ofs.write(ruamel.yaml.round_trip_dump(playbook, indent=4, block_seq_indent=2, default_flow_style=False))

        inventory_path = os.path.join(output_dir, 'hosts')
        with open(inventory_path, 'w') as ofs:
            for service_name, container_id in service_map.items():
                ofs.write('%s ansible_host="%s" ansible_python_interpreter="%s"\n' % (
                    service_name, container_id,
                    python_interpreter or engine.python_interpreter_path))

        if not os.path.exists(os.path.join(output_dir, 'files')):
            os.mkdir(os.path.join(output_dir, 'files'))
        if not os.path.exists(os.path.join(output_dir, 'templates')):
            os.mkdir(os.path.join(output_dir, 'templates'))
        rc = subprocess.call(['mount', '--bind', '/src',
                              os.path.join(output_dir, 'files')])
        if rc:
            raise OSError('Could not bind-mount /src into tmpdir')
        rc = subprocess.call(['mount', '--bind', '/src',
                              os.path.join(output_dir, 'templates')])
        if rc:
            raise OSError('Could not bind-mount /src into tmpdir')

        ansible_args = dict(inventory=inventory_path,
                            playbook=playbook_path,
                            debug_maybe='-vvvv' if debug else '',
                            engine_args=engine.ansible_args,
                            ansible_playbook=engine.ansible_exec_path,
                            ansible_options=ansible_options or '')
        # env = os.environ.copy()
        # env['ANSIBLE_REMOTE_TEMP'] = '/tmp/.ansible-${USER}/tmp'

        ansible_cmd = ('{ansible_playbook} '
                       '{debug_maybe} '
                       '{ansible_options} '
                       '-i {inventory} '
                       '{engine_args} '
                       '{playbook}').format(**ansible_args)
        logger.debug('Running Ansible Playbook', command=ansible_cmd, cwd='/src')
        process = subprocess.Popen(ansible_cmd,
                                   shell=True,
                                   bufsize=1,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT,
                                   cwd='/src',
                                   #env=env
                                   )

        log_iter = iter(process.stdout.readline, '')
        while process.returncode is None:
            try:
                plainLogger.info(log_iter.next().rstrip())
            except StopIteration:
                process.wait()
            finally:
                process.poll()

        return process.returncode
    finally:
        try:
            rc = subprocess.call(['unmount',
                                  os.path.join(output_dir, 'files')])
            rc = subprocess.call(['unmount',
                                  os.path.join(output_dir, 'templates')])
        except Exception:
            pass
        try:
            if remove_tmpdir:
                shutil.rmtree(output_dir, ignore_errors=True)
        except Exception:
            pass


@conductor_only
def apply_role_to_container(role, container_id, service_name, engine,
                            python_interpreter=None, ansible_options='',
                            debug=False):
    playbook = [
        {'hosts': service_name,
         'roles': [role]}
    ]

    container_metadata = engine.inspect_container(container_id)
    onbuild = container_metadata['Config']['OnBuild']
    # FIXME: Actually do stuff if onbuild is not null

    rc = run_playbook(playbook, engine, {service_name: container_id},
                      python_interpreter, ansible_options, debug)
    if rc:
        logger.error('Error applying role!', playbook=playbook, engine=engine,
            exit_code=rc)
    return rc


@conductor_only
def conductorcmd_build(engine_name, project_name, services, cache=True,
          python_interpreter=None, ansible_options='', debug=False, **kwargs):
    engine = load_engine(['BUILD'], engine_name, project_name, services)
    logger.info(u'%s integration engine loaded. Build starting.',
        engine.display_name, project=project_name)

    for service_name, service in services.items():
        logger.info(u'Building service...', service=service_name, project=project_name)
        cur_image_id = engine.get_image_id_by_tag(service['from'])
        # the fingerprint hash tracks cacheability
        fingerprint_hash = hashlib.sha256('%s::' % cur_image_id)
        logger.debug(u'Base fingerprint hash = %s', fingerprint_hash.hexdigest(),
                     service=service_name, hash=fingerprint_hash.hexdigest())
        cache_busted = not cache

        cur_container_id = engine.get_container_id_for_service(service_name)
        if cur_container_id:
            if engine.service_is_running(service_name):
                engine.stop_container(cur_container_id, forcefully=True)
            engine.delete_container(cur_container_id)

        if service.get('roles'):
            for role in service['roles']:
                role_fingerprint = get_role_fingerprint(role)
                fingerprint_hash.update(role_fingerprint)

                if not cache_busted:
                    logger.debug(u'Still trying to keep cache.', service=service_name)
                    cached_image_id = engine.get_image_id_by_fingerprint(
                        fingerprint_hash.hexdigest())
                    if cached_image_id:
                        # We can reuse the cached image
                        logger.debug(u'Cached layer found for service',
                                     service=service_name, fingerprint=fingerprint_hash.hexdigest())
                        cur_image_id = cached_image_id
                        logger.info(u'Applied role %s from cache', role,
                                    service=service_name, role=role)
                        continue
                    else:
                        cache_busted = True
                        logger.debug(u'Cache busted! No layer found',
                            service=service_name,
                            fingerprint=fingerprint_hash.hexdigest(),
                        )

                container_id = engine.run_container(
                    cur_image_id,
                    service_name,
                    name=engine.container_name_for_service(service_name),
                    user='root',
                    working_dir='/',
                    command='sh -c "while true; do sleep 1; '
                            'done"',
                    entrypoint=[],
                    environment=dict(LD_LIBRARY_PATH='/_usr/lib:/_usr/local/lib',
                                     CPATH='/_usr/include:/_usr/local/include',
                                     PATH='/usr/local/sbin:/usr/local/bin:'
                                          '/usr/sbin:/usr/bin:/sbin:/bin:'
                                          '/_usr/sbin:/_usr/bin:'
                                          '/_usr/local/sbin:/_usr/local/bin',
                                     PYTHONPATH='/_usr/lib/python2.7'),
                    volumes={engine.get_runtime_volume_id(): {'bind': '/_usr',
                                                              'mode': 'ro'}})
                while not engine.service_is_running(service_name):
                    time.sleep(0.2)
                logger.debug('Container running', id=container_id)

                rc = apply_role_to_container(role, container_id, service_name,
                                             engine,
                                             python_interpreter=python_interpreter,
                                             ansible_options=ansible_options,
                                             debug=debug)
                logger.debug('Playbook run finished.', exit_code=rc)
                if rc:
                    raise RuntimeError('Build failed.')
                logger.info(u'Applied role to service', service=service_name, role=role)

                engine.stop_container(container_id, forcefully=True)
                is_last_role = role is service['roles'][-1]
                image_id = engine.commit_role_as_layer(container_id,
                                                       service_name,
                                                       fingerprint_hash.hexdigest(),
                                                       service,
                                                       with_name=is_last_role)
                logger.info(u'Committed layer as image', service=service_name, image=image_id)
                engine.delete_container(container_id)
                cur_image_id = image_id
            # Tag the image also as latest:
            engine.tag_image_as_latest(service_name, cur_image_id)
            logger.info(u'Build complete.', service=service_name)
        else:
            logger.info(u'Service had no roles specified. Nothing to do.', service=service_name)
    logger.info(u'All images successfully built.')


@conductor_only
def conductorcmd_run(engine_name, project_name, services, **kwargs):
    engine = load_engine(['RUN'], engine_name, project_name, services)
    logger.info(u'Engine integration loaded. Preparing run.',
                engine=engine.display_name)

    engine.containers_built_for_services(services)
    logger.debug("In conductorcmd_run", kwargs=kwargs)
    playbook = engine.generate_orchestration_playbook()
    logger.debug("in conductorcmd_run", playbook=playbook)
    rc = run_playbook(playbook, engine, services, **kwargs)
    logger.info(u'All services running.', playbook_rc=rc)


@conductor_only
def conductorcmd_restart(engine_name, project_name, services, **kwargs):
    engine = load_engine(['RUN'], engine_name, project_name, services)
    logger.info(u'Engine integration loaded. Preparing to restart containers.',
                engine=engine.display_name)
    playbook = engine.generate_restart_playbook()
    rc = run_playbook(playbook, engine, services)
    logger.info(u'All services restarted.', playbook_rc=rc)


@conductor_only
def conductorcmd_stop(engine_name, project_name, services, **kwargs):
    engine = load_engine(['RUN'], engine_name, project_name, services)
    logger.info(u'Engine integration loaded. Preparing to stop all containers.',
                engine=engine.display_name)
    playbook = engine.generate_stop_playbook()
    rc = run_playbook(playbook, engine, services)
    logger.info(u'All services stopped.', playbook_rc=rc)


@conductor_only
def conductorcmd_destroy(engine_name, project_name, services, **kwargs):
    engine = load_engine(['RUN'], engine_name, project_name, services)
    logger.info(u'Engine integration loaded. Preparing to stop+delete all '
                u'containers and built images.',
                engine=engine.display_name)

    playbook = engine.generate_destroy_playbook()

    for service in services:
        image_name = engine.image_name_for_service(service)
        for image in engine.client.images.list(name=image_name):
            logger.debug('Found image for service', tags=image.tags, id=image.short_id)
            for tag in image.tags:
                logger.debug('Adding task to destroy image', tag=tag)
                playbook[0][u'tasks'].append({
                    u'docker_image': {
                        u'name': tag,
                        u'state': u'absent'
                    }
                })

    rc = run_playbook(playbook, engine, services)
    logger.info(u'All services destroyed.', playbook_rc=rc)


@conductor_only
def conductorcmd_deploy(engine_name, project_name, services, repository_data, playbook_dest, **kwargs):
    engine = load_engine(engine_name, project_name, services)
    logger.info(u'Engine integration loaded. Preparing deploy.',
                engine=engine.display_name())

    # Verify all images are built
    for service_name in services:
        logger.info(u'Verifying image for %s', service_name)
        image_id = engine.get_latest_image_id_for_service(service_name)
        if not image_id:
            logger.error(u'Missing image. Run "ansible-container build" '
                         u'to (re)create it.', service=service_name)
            raise RuntimeError(u'Run failed.')

    for service_name in services:
        logger.info(u'Pushing image for service', service=service_name, repo_name=repository_data['name'])
        image_id = engine.get_latest_image_id_for_service(service_name)
        engine.push_image(image_id, service_name, repository_data)

    logger.info(u'All images pushed.')

    playbook = engine.generate_orchestration_playbook(repository=repository_data)

    try:
        with open(os.path.join(playbook_dest, '%s.yml' % project_name), 'w') as ofs:
            ofs.write(ruamel.yaml.round_trip_dump(playbook, indent=4, block_seq_indent=2, default_flow_style=False))

    except OSError:
        logger.error(u'Failure writing deployment playbook', exc_info=True)
        raise


@conductor_only
def conductorcmd_install(engine_name, project_name, services, role, **kwargs):
    # FIXME: Port me from ac_galaxy.py
    pass


@conductor_only
def conductorcmd_push(engine_name, project_name, services, **kwargs):
    """ Push images to a registry """
    username = kwargs.pop('username')
    password = kwargs.pop('password')
    email = kwargs.pop('email')
    url = kwargs.pop('url')
    namespace = kwargs.pop('namespace')
    tag = kwargs.pop('tag')
    config_path = kwargs.pop('config_path')

    engine = load_engine(['PUSH', 'LOGIN'], engine_name, project_name, services)
    logger.info(u'Engine integration loaded. Preparing push.',
                engine=engine.display_name)

    # Verify that we can authenticate with the registry
    username, password = engine.login(username, password, email, url, config_path)

    repo_data = {
        'url': url,
        'namespace': namespace or username,
        'tag': tag,
        'username': username,
        'password': password
    }

    # Push each image that has been built using Ansible roles
    for service_name, service_config in services.items():
        if service_config.get('roles'):
            # if the service has roles, it's an image we should push
            image_id = engine.get_latest_image_id_for_service(service_name)
            engine.push(image_id, service_name, repo_data)
