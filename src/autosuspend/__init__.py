#!/usr/bin/env python3
"""A daemon to suspend a system on inactivity."""

import abc
import argparse
import configparser
import copy
import datetime
import functools
import glob
import logging
import logging.config
import os
import os.path
import pwd
import re
import socket
import subprocess
import time
from typing import (Callable,
                    Iterable,
                    IO,
                    List,
                    Optional,
                    Sequence,
                    Type,
                    TypeVar)

import psutil


# pylint: disable=invalid-name
_logger = logging.getLogger()
# pylint: enable=invalid-name


class ConfigurationError(RuntimeError):
    """Indicates an error in the configuration of a :class:`Check`."""

    pass


class TemporaryCheckError(RuntimeError):
    """Indicates a temporary error while performing a check.

    Such an error can be ignored for some time since it might recover
    automatically.
    """

    pass


class SevereCheckError(RuntimeError):
    """Indicates a sever check error that will probably not recover.

    There no hope this situation recovers.
    """

    pass


class Check(object):
    """Base class for all kinds of checks.

    Subclasses must call this class' ``__init__`` method.

    Args:
        name (str):
            Configured name of the check
    """

    @classmethod
    @abc.abstractmethod
    def create(cls, name: str, config: configparser.SectionProxy) -> 'Check':
        """Create a new check instance from the provided configuration.

        Args:
            name (str):
                user-defined name for the check
            config (configparser.SectionProxy):
                config parser section with the configuration for this check

        Raises:
            ConfigurationError:
                Configuration for this check is inappropriate

        """
        pass

    def __init__(self, name: str = None) -> None:
        if name:
            self.name = name
        else:
            self.name = self.__class__.__name__
        self.logger = logging.getLogger(
            'check.{}'.format(self.name))

    def __str__(self):
        return '{name}[class={clazz}]'.format(name=self.name,
                                              clazz=self.__class__.__name__)


class Activity(Check):
    """Base class for activity checks.

    Subclasses must call this class' __init__ method.
    """

    @abc.abstractmethod
    def check(self) -> Optional[str]:
        """Determine if system activity exists that prevents suspending.

        Returns:
            str:
                A string describing which condition currently prevents sleep,
                else ``None``.

        Raises:
            TemporaryCheckError:
                Check execution currently fails but might recover later
            SevereCheckError:
                Check executions fails severely
        """
        pass

    def __str__(self) -> str:
        return '{name}[class={clazz}]'.format(name=self.name,
                                              clazz=self.__class__.__name__)


class Wakeup(Check):
    """Represents a check for potential wake up points."""

    @abc.abstractmethod
    def check(self,
              timestamp: datetime.datetime) -> Optional[datetime.datetime]:
        """Indicate if a wakeup has to be scheduled for this check.

        Args:
            timestamp:
                the time at which the call to the wakeup check is made

        Returns:
            a datetime describing when the system needs to be running again or
            ``None`` if no wakeup is required. Use timezone aware datetimes.

        Raises:
            TemporaryCheckError:
                Check execution currently fails but might recover later
            SevereCheckError:
                Check executions fails severely
        """
        pass


class WakeupFile(Wakeup):
    """Determines scheduled wake ups from the contents of a file on disk.

    File contents are interpreted as a Unix timestamp in seconds UTC.
    """

    @classmethod
    def create(cls, name, config):
        try:
            path = config['path']
            return cls(name, path)
        except KeyError:
            raise ConfigurationError('Missing option path')

    def __init__(self, name, path):
        Check.__init__(self, name)
        self._path = path

    def check(self, timestamp):
        try:
            with open(self._path, 'r') as time_file:
                return datetime.datetime.fromtimestamp(
                    float(time_file.readlines()[0].strip()),
                    datetime.timezone.utc)
        except FileNotFoundError:
            # this is ok
            pass
        except (ValueError, PermissionError, IOError) as error:
            raise TemporaryCheckError(error)


class CommandMixin(object):
    """Mixin for configuring checks based on external commands."""

    @classmethod
    def create(cls, name, config):
        try:
            return cls(name, config['command'].strip())
        except KeyError as error:
            raise ConfigurationError('Missing command specification')

    def __init__(self, command):
        self._command = command


class WakeupCommand(CommandMixin, Wakeup):
    """Determine wake up times based on an external command.

    The called command must return a timestamp in UTC or nothing in case no
    wake up is planned.
    """

    def __init__(self, name, command):
        CommandMixin.__init__(self, command)
        Wakeup.__init__(self, name)

    def check(self, timestamp):
        try:
            output = subprocess.check_output(self._command,
                                             shell=True).splitlines()[0]
            self.logger.debug('Command %s succeeded with output %s',
                              self._command, output)
            if output.strip():
                return datetime.datetime.fromtimestamp(
                    float(output.strip()),
                    datetime.timezone.utc)

        except (subprocess.CalledProcessError, ValueError) as error:
            raise TemporaryCheckError(error) from error


class XPathMixin(object):

    @classmethod
    def create(cls, name, config, **kwargs):
        from lxml import etree
        try:
            xpath = config['xpath'].strip()
            # validate the expression
            try:
                etree.fromstring('<a></a>').xpath(xpath)
            except etree.XPathEvalError:
                raise ConfigurationError('Invalid xpath expression: ' + xpath)
            timeout = config.getint('timeout', fallback=5)
            return cls(name, xpath, config['url'], timeout, **kwargs)
        except ValueError as error:
            raise ConfigurationError('Configuration error ' + str(error))
        except KeyError as error:
            raise ConfigurationError('No ' + str(error) +
                                     ' entry defined for the XPath check')

    def __init__(self, xpath, url, timeout):
        self._xpath = xpath
        self._url = url
        self._timeout = timeout

    def evaluate(self):
        import requests
        import requests.exceptions
        from lxml import etree

        try:
            reply = requests.get(self._url, timeout=self._timeout).content
            root = etree.fromstring(reply)
            return root.xpath(self._xpath)
        except requests.exceptions.RequestException as error:
            raise TemporaryCheckError(error)
        except etree.XMLSyntaxError as error:
            raise TemporaryCheckError(error)


class WakeupXPath(XPathMixin, Wakeup):
    """Determine wake up times from a network resource using XPath expressions.

    The matched results are expected to represent timestamps in seconds UTC.
    """

    def __init__(self, name, url, xpath, timeout):
        Wakeup.__init__(self, name)
        XPathMixin.__init__(self, url, xpath, timeout)

    def convert_result(self, result, timestamp):
        return datetime.datetime.fromtimestamp(float(result),
                                               datetime.timezone.utc)

    def check(self, timestamp):
        matches = self.evaluate()
        try:
            if matches:
                return min([self.convert_result(m, timestamp)
                            for m in matches])
        except TypeError as error:
            raise TemporaryCheckError(
                'XPath returned a result that is not a string: ' + str(error))
        except ValueError as error:
            raise TemporaryCheckError('Result cannot be parsed: ' + str(error))


class WakeupXPathDelta(WakeupXPath):

    UNITS = ['days', 'seconds', 'microseconds', 'milliseconds',
             'minutes', 'hours', 'weeks']

    @classmethod
    def create(cls, name, config):
        try:
            return super(WakeupXPath, cls).create(
                name, config,
                unit=config.get('unit', fallback='minutes'))
        except ValueError as error:
            raise ConfigurationError(str(error))

    def __init__(self, name, url, xpath, timeout, unit='minutes'):
        if unit not in self.UNITS:
            raise ValueError('Unsupported unit')
        WakeupXPath.__init__(self, name, url, xpath, timeout)
        self._unit = unit

    def convert_result(self, result, timestamp):
        kwargs = {}
        kwargs[self._unit] = float(result)
        return timestamp + datetime.timedelta(**kwargs)


class ActiveConnection(Activity):
    """Checks if a client connection exists on specified ports."""

    @classmethod
    def create(cls, name, config):
        try:
            ports = config['ports']
            ports = ports.split(',')
            ports = [p.strip() for p in ports]
            ports = set([int(p) for p in ports])
            return cls(name, ports)
        except KeyError:
            raise ConfigurationError('Missing option ports')
        except ValueError:
            raise ConfigurationError('Ports must be integers')

    def __init__(self, name, ports):
        Activity.__init__(self, name)
        self._ports = ports

    def check(self):
        own_addresses = [(item.family, item.address)
                         for sublist in psutil.net_if_addrs().values()
                         for item in sublist]
        connected = [c.laddr[1]
                     for c in psutil.net_connections()
                     if ((c.family, c.laddr[0]) in own_addresses and
                         c.status == 'ESTABLISHED' and
                         c.laddr[1] in self._ports)]
        if connected:
            return 'Ports {} are connected'.format(connected)


class ExternalCommand(CommandMixin, Activity):

    def __init__(self, name, command):
        CommandMixin.__init__(self, command)
        Check.__init__(self, name)

    def check(self):
        try:
            subprocess.check_call(self._command, shell=True)
            return 'Command {} succeeded'.format(self._command)
        except subprocess.CalledProcessError as error:
            return None


class Kodi(Activity):

    @classmethod
    def create(cls, name, config):
        try:
            url = config.get('url', fallback='http://localhost:8080/jsonrpc')
            timeout = config.getint('timeout', fallback=5)
            return cls(name, url, timeout)
        except ValueError as error:
            raise ConfigurationError(
                'Url or timeout configuration wrong: {}'.format(error))

    def __init__(self, name, url, timeout):
        Check.__init__(self, name)
        self._url = url
        self._timeout = timeout

    def check(self):
        import requests
        import requests.exceptions

        try:
            reply = requests.get(self._url +
                                 '?request={"jsonrpc": "2.0", '
                                 '"id": 1, '
                                 '"method": "Player.GetActivePlayers"}',
                                 timeout=self._timeout).json()
            if 'result' not in reply:
                raise TemporaryCheckError('No result array in reply')
            if reply['result']:
                return "Kodi currently playing"
            else:
                return None
        except requests.exceptions.RequestException as error:
            raise TemporaryCheckError(error)


class Load(Activity):

    @classmethod
    def create(cls, name, config):
        try:
            return cls(name,
                       config.getfloat('threshold', fallback=2.5))
        except ValueError as error:
            raise ConfigurationError(
                'Unable to parse threshold as float: {}'.format(error))

    def __init__(self, name, threshold):
        Check.__init__(self, name)
        self._threshold = threshold

    def check(self):
        loadcurrent = os.getloadavg()[1]
        self.logger.debug("Load: %s", loadcurrent)
        if loadcurrent > self._threshold:
            return 'Load {} > threshold {}'.format(loadcurrent,
                                                   self._threshold)
        else:
            return None


class Mpd(Activity):

    @classmethod
    def create(cls, name, config):
        try:
            host = config.get('host', fallback='localhost')
            port = config.getint('port', fallback=6600)
            timeout = config.getint('timeout', fallback=5)
            return cls(name, host, port, timeout)
        except ValueError as error:
            raise ConfigurationError(
                'Host port or timeout configuration wrong: {}'.format(error))

    def __init__(self, name, host, port, timeout):
        Check.__init__(self, name)
        self._host = host
        self._port = port
        self._timeout = timeout

    def _get_state(self):
        from mpd import MPDClient
        client = MPDClient()
        client.timeout = self._timeout
        client.connect(self._host, self._port)
        state = client.status()
        client.close()
        client.disconnect()
        return state

    def check(self):
        try:
            state = self._get_state()
            if state['state'] == 'play':
                return 'MPD currently playing'
            else:
                return None
        except (ConnectionError,
                ConnectionRefusedError,
                socket.timeout,
                socket.gaierror) as error:
            raise TemporaryCheckError(error)


class NetworkBandwidth(Activity):

    @classmethod
    def create(cls, name, config):
        try:
            interfaces = config['interfaces']
            interfaces = interfaces.split(',')
            interfaces = [i.strip() for i in interfaces if i.strip()]
            if not interfaces:
                raise ConfigurationError('No interfaces configured')
            host_interfaces = psutil.net_if_addrs().keys()
            for interface in interfaces:
                if interface not in host_interfaces:
                    raise ConfigurationError(
                        'Network interface {} does not exist'.format(
                            interface))
            threshold_send = config.getfloat('threshold_send',
                                             fallback=100)
            threshold_receive = config.getfloat('threshold_receive',
                                                fallback=100)
            return cls(name, interfaces, threshold_send, threshold_receive)
        except KeyError as error:
            raise ConfigurationError(
                'Missing configuration key: {}'.format(error))
        except ValueError as error:
            raise ConfigurationError(
                'Threshold in wrong format: {}'.format(error))

    def __init__(self, name, interfaces, threshold_send, threshold_receive):
        Check.__init__(self, name)
        self._interfaces = interfaces
        self._threshold_send = threshold_send
        self._threshold_receive = threshold_receive
        self._previous_values = psutil.net_io_counters(pernic=True)
        self._previous_time = time.time()

    def check(self):
        new_values = psutil.net_io_counters(pernic=True)
        new_time = time.time()
        for interface in self._interfaces:
            if interface not in new_values or \
                    interface not in self._previous_values:
                raise TemporaryCheckError(
                    'Interface {} is missing'.format(interface))

            # send direction
            delta_send = new_values[interface].bytes_sent - \
                self._previous_values[interface].bytes_sent
            rate_send = delta_send / (new_time - self._previous_time)
            if rate_send > self._threshold_send:
                return 'Interface {} sending rate {} byte/s '\
                    'higher than threshold {}'.format(
                        interface, rate_send, self._threshold_send)

            delta_receive = new_values[interface].bytes_recv - \
                self._previous_values[interface].bytes_recv
            rate_receive = delta_receive / (new_time - self._previous_time)
            if rate_receive > self._threshold_receive:
                return 'Interface {} receive rate {} byte/s '\
                    'higher than threshold {}'.format(
                        interface, rate_receive, self._threshold_receive)


class Ping(Activity):
    """Check if one or several hosts are reachable via ping."""

    @classmethod
    def create(cls, name, config):
        try:
            hosts = config['hosts'].split(',')
            hosts = [h.strip() for h in hosts]
            return cls(name, hosts)
        except KeyError as error:
            raise ConfigurationError(
                'Unable to determine hosts to ping: {}'.format(error))

    def __init__(self, name, hosts):
        Check.__init__(self, name)
        self._hosts = hosts

    def check(self):
        for host in self._hosts:
            cmd = ['ping', '-q', '-c', '1', host]
            if subprocess.call(cmd,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL) == 0:
                self.logger.debug("host " + host + " appears to be up")
                return 'Host {} is up'.format(host)
        return None


class Processes(Activity):

    @classmethod
    def create(cls, name, config):
        try:
            processes = config['processes'].split(',')
            processes = [p.strip() for p in processes]
            return cls(name, processes)
        except KeyError:
            raise ConfigurationError('No processes to check specified')

    def __init__(self, name, processes):
        Check.__init__(self, name)
        self._processes = processes

    def check(self):
        for proc in psutil.process_iter():
            try:
                pinfo = proc.name()
                for name in self._processes:
                    if pinfo == name:
                        return 'Process {} is running'.format(name)
            except psutil.NoSuchProcess:
                pass
        return None


class Smb(Activity):

    @classmethod
    def create(cls, name, config):
        return cls(name)

    def check(self):
        try:
            status_output = subprocess.check_output(
                ['smbstatus', '-b']).decode('utf-8')
        except subprocess.CalledProcessError as error:
            raise SevereCheckError(error)

        self.logger.debug('Received status output:\n%s',
                          status_output)

        connections = []
        start_seen = False
        for line in status_output.splitlines():
            if start_seen:
                connections.append(line)
            else:
                if line.startswith('----'):
                    start_seen = True

        if connections:
            return 'SMB clients are connected:\n{}'.format(
                '\n'.join(connections))
        else:
            return None


class Users(Activity):

    @classmethod
    def create(cls, name, config):
        try:
            user_regex = re.compile(
                config.get('name', fallback='.*'))
            terminal_regex = re.compile(
                config.get('terminal', fallback='.*'))
            host_regex = re.compile(
                config.get('host', fallback='.*'))
            return cls(name, user_regex, terminal_regex, host_regex)
        except re.error as error:
            raise ConfigurationError(
                'Regular expression is invalid: {}'.format(error))

    def __init__(self, name, user_regex, terminal_regex, host_regex):
        Activity.__init__(self, name)
        self._user_regex = user_regex
        self._terminal_regex = terminal_regex
        self._host_regex = host_regex

    def check(self):
        for entry in psutil.users():
            if self._user_regex.fullmatch(entry.name) is not None and \
                    self._terminal_regex.fullmatch(
                        entry.terminal) is not None and \
                    self._host_regex.fullmatch(entry.host) is not None:
                self.logger.debug('User %s on terminal %s from host %s '
                                  'matches criteria.', entry.name,
                                  entry.terminal, entry.host)
                return 'User {user} is logged in on terminal {terminal} ' \
                    'from {host} since {started}'.format(
                        user=entry.name, terminal=entry.terminal,
                        host=entry.host, started=entry.started)
        return None


def _list_logind_sessions():
    """List running logind sessions and their properties.

    Returns:
        list of (session_id, properties dict):
            A list with tuples of sessions ids and their associated properties
            represented as dicts.
    """
    import dbus
    bus = dbus.SystemBus()
    login1 = bus.get_object("org.freedesktop.login1",
                            "/org/freedesktop/login1")

    sessions = login1.ListSessions(
        dbus_interface='org.freedesktop.login1.Manager')

    results = []
    for session_id, path in [(s[0], s[4]) for s in sessions]:
        session = bus.get_object('org.freedesktop.login1', path)
        properties_interface = dbus.Interface(
            session, 'org.freedesktop.DBus.Properties')
        properties = properties_interface.GetAll(
            'org.freedesktop.login1.Session')
        results.append((session_id, properties))

    return results


class XIdleTime(Activity):
    """Check that local X display have been idle long enough."""

    @classmethod
    def create(cls, name, config):
        try:
            return cls(name, config.getint('timeout', fallback=600),
                       config.get('method', fallback='sockets'),
                       re.compile(config.get('ignore_if_process',
                                             fallback=r'a^')),
                       re.compile(config.get('ignore_users',
                                             fallback=r'a^')))
        except re.error as error:
            raise ConfigurationError(
                'Regular expression is invalid: {}'.format(error))
        except ValueError as error:
            raise ConfigurationError(
                'Unable to parse configuration: {}'.format(error))

    def __init__(self, name, timeout, method,
                 ignore_process_re, ignore_users_re):
        Activity.__init__(self, name)
        self._timeout = timeout
        if method == 'sockets':
            self._provide_sessions = self._list_sessions_sockets
        elif method == 'logind':
            self._provide_sessions = self._list_sessions_logind
        else:
            raise ValueError(
                "Unknown session discovery method {}".format(method))
        self._ignore_process_re = ignore_process_re
        self._ignore_users_re = ignore_users_re

    def _list_sessions_sockets(self):
        """List running X sessions by iterating the X sockets.

        This method assumes that X servers are run under the users using the
        server.
        """
        sockets = glob.glob('/tmp/.X11-unix/X*')
        self.logger.debug('Found sockets: %s', sockets)

        results = []
        for sock in sockets:
            # determine the number of the X display
            try:
                display = int(sock[len('/tmp/.X11-unix/X'):])
            except ValueError as error:
                self.logger.warning(
                    'Cannot parse display number from socket %s. Skipping.',
                    sock, exc_info=True)
                continue

            # determine the user of the display
            try:
                user = pwd.getpwuid(os.stat(sock).st_uid).pw_name
            except (FileNotFoundError, KeyError) as error:
                self.logger.warning(
                    'Cannot get the owning user from socket %s. Skipping.',
                    sock, exc_info=True)
                continue

            results.append((display, user))

        return results

    def _list_sessions_logind(self):
        """List running X sessions using logind.

        This method assumes that a ``Display`` variable is set in the logind
        sessions.
        """
        results = []
        for session_id, properties in _list_logind_sessions():
            if 'Name' in properties and 'Display' in properties:
                try:
                    results.append(
                        (int(properties['Display'].replace(':', '')),
                         str(properties['Name'])))
                except ValueError as e:
                    self.logger.warn(
                        'Unable to parse display from session properties %s',
                        properties, exc_info=True)
            else:
                self.logger.debug(
                    'Skipping session %s because it does not contain '
                    'a user name and a display', session_id)
        return results

    def _is_skip_process_running(self, user):
        user_processes = []
        for process in psutil.process_iter():
            try:
                if process.username() == user:
                    user_processes.append(process.name())
            except (psutil.NoSuchProcess,
                    psutil.ZombieProcess,
                    psutil.AccessDenied):
                # ignore processes which have disappeared etc.
                pass

        for process in user_processes:
            if self._ignore_process_re.match(process) is not None:
                self.logger.debug(
                    "Process %s with pid %s matches the ignore regex '%s'."
                    " Skipping idle time check for this user.",
                    process.name(), process.pid, self._ignore_process_re)
                return True

        return False

    def check(self):
        for display, user in self._provide_sessions():
            self.logger.info('Checking display %s of user %s', display, user)

            # check whether this users should be ignored completely
            if self._ignore_users_re.match(user) is not None:
                self.logger.debug("Skipping user '%s' due to request", user)
                continue

            # check whether any of the running processes of this user matches
            # the ignore regular expression. In that case we skip idletime
            # checking because we assume the user has a process running that
            # inevitably tampers with the idle time.
            if self._is_skip_process_running(user):
                continue

            # prepare the environment for the xprintidle call
            env = copy.deepcopy(os.environ)
            env['DISPLAY'] = ':{}'.format(display)
            env['XAUTHORITY'] = os.path.join(os.path.expanduser('~' + user),
                                             '.Xauthority')

            try:
                idle_time = subprocess.check_output(
                    ['sudo', '-u', user, 'xprintidle'], env=env)
                idle_time = float(idle_time.strip()) / 1000.0
            except (subprocess.CalledProcessError, ValueError) as error:
                self.logger.warning(
                    'Unable to determine the idle time for display %s.',
                    display, exc_info=True)
                raise TemporaryCheckError(error)

            self.logger.debug(
                'Idle time for display %s of user %s is %s seconds.',
                display, user, idle_time)

            if idle_time < self._timeout:
                return 'X session {} of user {} ' \
                    'has idle time {} < threshold {}'.format(
                        display, user, idle_time, self._timeout)

        return None


class LogindSessionsIdle(Activity):
    """Prevents suspending in case a logind session is marked not idle.

    The decision is based on the ``IdleHint`` property of logind sessions.
    """

    @classmethod
    def create(cls, name, config):
        types = config.get('types', fallback='tty,x11,wayland')
        types = [t.strip() for t in types.split(',')]
        states = config.get('states', fallback='active,online')
        states = [t.strip() for t in states.split(',')]
        return cls(name, types, states)

    def __init__(self, name, types, states):
        Activity.__init__(self, name)
        self._types = types
        self._states = states

    def check(self):
        for session_id, properties in _list_logind_sessions():
            self.logger.debug('Session %s properties: %s',
                              session_id, properties)

            if properties['Type'] not in self._types:
                self.logger.debug('Ignoring session of wrong type %s',
                                  properties['type'])
                continue
            if properties['State'] not in self._states:
                self.logger.debug('Ignoring session because its state is %s',
                                  properties['State'])
                continue

            if properties['IdleHint'] == 'no':
                return 'Login session {} is not idle'.format(
                    session_id, properties['IdleHint'])

        return None


class XPath(XPathMixin, Activity):

    def __init__(self, name, url, xpath, timeout):
        Activity.__init__(self, name)
        XPathMixin.__init__(self, url, xpath, timeout)

    def check(self):
        if self.evaluate():
            return "XPath matches for url " + self._url


def execute_suspend(command: str, wakeup_at: Optional[datetime.datetime]):
    """Suspend the system by calling the specified command.

    Args:
        command:
            The command to execute, which will be executed using shell
            execution
        wakeup_at:
            potential next wakeup time. Only informative.
    """
    _logger.info('Suspending using command: %s', command)
    try:
        subprocess.check_call(command, shell=True)
    except subprocess.CalledProcessError:
        _logger.warning('Unable to execute suspend command: %s', command,
                        exc_info=True)


def notify_suspend(command_wakeup_template: Optional[str],
                   command_no_wakeup: Optional[str],
                   wakeup_at: Optional[datetime.datetime]):
    """Call a command to notify on suspending.

    Args:
        command_no_wakeup_template:
            A template for the command to execute in case a wakeup is
            scheduled.
            It will be executed using shell execution.
            The template is processed with string formatting to include
            information on a potentially scheduled wakeup.
            Notifications can be disable by providing ``None`` here.
        command_no_wakeup:
            Command to execute for notification in case no wake up is
            scheduled.
            Will be executed using shell execution.
        wakeup_at:
            if not ``None``, this is the time the system will wake up again
    """

    def safe_exec(command):
        _logger.info('Notifying using command: %s', command)
        try:
            subprocess.check_call(command, shell=True)
        except subprocess.CalledProcessError:
            _logger.warning('Unable to execute notification command: %s',
                            command, exc_info=True)

    if wakeup_at and command_wakeup_template:
        command = command_wakeup_template.format(
            timestamp=wakeup_at.timestamp(),
            iso=wakeup_at.isoformat())
        safe_exec(command)
    elif not wakeup_at and command_no_wakeup:
        safe_exec(command_no_wakeup)
    else:
        _logger.info('No suitable notification command configured.')


def notify_and_suspend(suspend_cmd: str,
                       notify_cmd_wakeup_template: Optional[str],
                       notify_cmd_no_wakeup: Optional[str],
                       wakeup_at: Optional[datetime.datetime]):
    notify_suspend(notify_cmd_wakeup_template, notify_cmd_no_wakeup, wakeup_at)
    execute_suspend(suspend_cmd, wakeup_at)


def schedule_wakeup(command_template: str, wakeup_at: datetime.datetime):
    command = command_template.format(timestamp=wakeup_at.timestamp(),
                                      iso=wakeup_at.isoformat())
    _logger.info('Scheduling wakeup using command: %s', command)
    try:
        subprocess.check_call(command, shell=True)
    except subprocess.CalledProcessError:
        _logger.warning('Unable to execute wakeup scheduling command: %s',
                        command, exc_info=True)


def execute_checks(checks: Iterable[Activity],
                   all_checks: bool,
                   logger) -> bool:
    """Execute the provided checks sequentially.

    Args:
        checks:
            the checks to execute
        all_checks:
            if ``True``, execute all checks even if a previous one already
            matched.

    Return:
        ``True`` if a check matched
    """
    matched = False
    for check in checks:
        logger.debug('Executing check %s', check.name)
        try:
            result = check.check()
            if result is not None:
                logger.info('Check %s matched. Reason: %s', check.name, result)
                matched = True
                if not all_checks:
                    logger.debug('Skipping further checks')
                    break
        except TemporaryCheckError:
            logger.warning('Check %s failed. Ignoring...', check,
                           exc_info=True)
    return matched


def execute_wakeups(wakeups: Iterable[Wakeup],
                    timestamp: datetime.datetime,
                    logger) -> Optional[datetime.datetime]:

    wakeup_at = None
    for wakeup in wakeups:
        try:
            this_at = wakeup.check(timestamp)

            # sanity checks
            if this_at is None:
                continue
            if this_at <= timestamp:
                logger.warning('Wakeup %s returned a scheduled wakeup at %s, '
                               'which is earlier than the current time %s. '
                               'Ignoring.',
                               wakeup, this_at, timestamp)
                continue

            if wakeup_at is None:
                wakeup_at = this_at
            else:
                wakeup_at = min(this_at, wakeup_at)
        except TemporaryCheckError:
            logger.warning('Wakeup %s failed. Ignoring...', wakeup,
                           exc_info=True)

    return wakeup_at


class Processor(object):
    """Implements the logic for triggering suspension.

    Args:
        activities:
            the activity checks to execute
        wakeups:
            the wakeup checks to execute
        idle_time:
            the required amount of time the system has to be idle before
            suspension is triggered in seconds
        min_sleep_time:
            the minimum time the system has to sleep before it is woken up
            again in seconds.
        wakeup_delta:
            wake up this amount of seconds before the scheduled wake up time.
        sleep_fn:
            a callable that triggers suspension
        wakeup_fn:
            a callable that schedules the wakeup at the specified time in UTC
            seconds
        notify_fn:
            a callable that is called before suspending.
            One argument gives the scheduled wakeup time or ``None``.
        all_activities:
            if ``True``, execute all activity checks even if a previous one
            already matched.
    """

    def __init__(self,
                 activities: List[Activity],
                 wakeups: List[Wakeup],
                 idle_time: float,
                 min_sleep_time: float,
                 wakeup_delta: float,
                 sleep_fn: Callable,
                 wakeup_fn: Callable[[datetime.datetime], None],
                 all_activities: bool) -> None:
        self._logger = logging.getLogger('Processor')
        self._activities = activities
        self._wakeups = wakeups
        self._idle_time = idle_time
        self._min_sleep_time = min_sleep_time
        self._wakeup_delta = wakeup_delta
        self._sleep_fn = sleep_fn
        self._wakeup_fn = wakeup_fn
        self._all_activities = all_activities
        self._idle_since = None  # type: Optional[datetime.datetime]

    def _reset_state(self, reason: str) -> None:
        self._logger.info('%s. Resetting state', reason)
        self._idle_since = None

    def iteration(self, timestamp: datetime.datetime, just_woke_up: bool):
        self._logger.info('Starting new check iteration')

        # determine system activity
        active = execute_checks(self._activities, self._all_activities,
                                self._logger)
        self._logger.debug('All activity checks have been executed. '
                           'Active: %s', active)
        # determine potential wake ups
        wakeup_at = execute_wakeups(self._wakeups, timestamp, self._logger)
        self._logger.debug('Checks report, system should wake up at %s',
                           wakeup_at)
        if wakeup_at is not None:
            wakeup_at -= datetime.timedelta(seconds=self._wakeup_delta)
        self._logger.debug('With delta, system should wake up at %s',
                           wakeup_at)

        # exit in case something prevents suspension
        if just_woke_up:
            self._reset_state('Just woke up from suspension')
            return
        if active:
            self._reset_state('System is active')
            return

        # set idle timestamp if required
        if self._idle_since is None:
            self._idle_since = timestamp

        self._logger.info('System is idle since %s', self._idle_since)

        # determine if systems is idle long enough
        self._logger.debug('Idle seconds: %s',
                           (timestamp - self._idle_since).total_seconds())
        if (timestamp - self._idle_since).total_seconds() > self._idle_time:
            self._logger.info('System is idle long enough.')

            # idle time would be reached, handle wake up
            if wakeup_at is not None:
                wakeup_in = wakeup_at - timestamp
                if wakeup_in.total_seconds() < self._min_sleep_time:
                    self._logger.info('Would wake up in %s seconds, which is '
                                      'below the minimum amount of %s s. '
                                      'Not suspending.',
                                      wakeup_in.total_seconds(),
                                      self._min_sleep_time)
                    return

                # schedule wakeup
                self._logger.info('Scheduling wakeup at %s', wakeup_at)
                self._wakeup_fn(wakeup_at)

            self._reset_state('Going to suspend')
            self._sleep_fn(wakeup_at)
        else:
            self._logger.info('Desired idle time of %s s not reached yet.',
                              self._idle_time)


def loop(processor: Processor,
         interval: int,
         run_for: Optional[int],
         woke_up_file: str) -> None:
    """Run the main loop of the daemon.

    Args:
        processor:
            the processor to use for handling the suspension computations
        interval:
            the length of one iteration of the main loop in seconds
        idle_time:
            the required amount of time the system has to be idle before
            suspension is triggered
        sleep_fn:
            a callable that triggers suspension
        run_for:
            if specified, run the main loop for the specified amount of seconds
            before terminating (approximately)
    """

    start_time = datetime.datetime.now(datetime.timezone.utc)
    while (run_for is None) or (datetime.datetime.now(datetime.timezone.utc) <
                                (start_time + datetime.timedelta(
                                    seconds=run_for))):

        just_woke_up = os.path.isfile(woke_up_file)
        if just_woke_up:
            os.remove(woke_up_file)

        processor.iteration(datetime.datetime.now(datetime.timezone.utc),
                            just_woke_up)

        time.sleep(interval)


CheckType = TypeVar('CheckType', bound=Check)


def set_up_checks(config: configparser.ConfigParser,
                  prefix: str,
                  target_class: Type[CheckType],
                  error_none: bool = False) -> List[CheckType]:
    """Set up :py.class:`Check` instances from a given configuration.

    Args:
        config:
            the configuration to use
        prefix:
            The prefix of sections in the configuration file to use for
            creating instances.
        target_class:
            the base class to check new instance agains
        error_none:
            Raise an error if nothing was configured?
    """
    configured_checks = []  # type: List[CheckType]

    check_section = [s for s in config.sections()
                     if s.startswith('{}.'.format(prefix))]
    for section in check_section:
        name = section[len('{}.'.format(prefix)):]
        # legacy method to determine the check name from the section header
        class_name = name
        # if there is an explicit class, use that one with higher priority
        if 'class' in config[section]:
            class_name = config[section]['class']
        enabled = config.getboolean(section, 'enabled', fallback=False)

        if not enabled:
            _logger.debug('Skipping disabled check {}'.format(name))
            continue

        _logger.info('Configuring check {} with class {}'.format(
            name, class_name))
        try:
            klass = globals()[class_name]
        except KeyError:
            raise ConfigurationError(
                'Cannot create check named {}: Class does not exist'.format(
                    class_name))

        check = klass.create(name, config[section])
        if not isinstance(check, target_class):
            raise ConfigurationError(
                'Check {} is not a correct {} instance'.format(
                    check, target_class.__name__))
        _logger.debug('Created check instance {}'.format(check))
        configured_checks.append(check)

    if not configured_checks and error_none:
        raise ConfigurationError('No checks enabled')

    return configured_checks


def parse_config(config_file: Iterable[str]):
    """Parse the configuration file.

    Args:
        config_file:
            The file to parse
    """
    _logger.debug('Reading config file %s', config_file)
    config = configparser.ConfigParser(
        interpolation=configparser.ExtendedInterpolation())
    config.read_file(config_file)
    _logger.debug('Parsed config file: %s', config)
    return config


def parse_arguments(args: Optional[Sequence[str]]) -> argparse.Namespace:
    """Parse command line arguments.

    Args:
        args:
            if specified, use the provided arguments instead of the default
            ones determined via the :module:`sys` module.
    """
    parser = argparse.ArgumentParser(
        description='Automatically suspends a server '
                    'based on several criteria',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    IO  # for making pyflakes happy
    default_config = None  # type: Optional[IO[str]]
    try:
        default_config = open('/etc/autosuspend.conf', 'r')
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        pass
    parser.add_argument(
        '-c', '--config',
        dest='config_file',
        type=argparse.FileType('r'),
        default=default_config,
        required=default_config is None,
        metavar='FILE',
        help='The config file to use')
    parser.add_argument(
        '-a', '--allchecks',
        dest='all_checks',
        default=False,
        action='store_true',
        help='Execute all checks even if one has already prevented '
             'the system from going to sleep. Useful to debug individual '
             'checks.')
    parser.add_argument(
        '-r', '--runfor',
        dest='run_for',
        type=float,
        default=None,
        metavar='SEC',
        help="If set, run for the specified amount of seconds before exiting "
             "instead of endless execution.")
    parser.add_argument(
        '-l', '--logging',
        type=argparse.FileType('r'),
        nargs='?',
        default=False,
        const=True,
        metavar='FILE',
        help='Configures the python logging system. If used '
             'without an argument, all logging is enabled to '
             'the console. If used with an argument, the '
             'configuration is read from the specified file.')

    result = parser.parse_args(args)

    _logger.debug('Parsed command line arguments %s', result)

    return result


def configure_logging(file_or_flag):
    """Configure the python :mod:`logging` system.

    If the provided argument is a `file` instance, try to use the
    pointed to file as a configuration for the logging system. Otherwise,
    if the given argument evaluates to :class:True:, use a default
    configuration with many logging messages. If everything fails, just log
    starting from the warning level.

    Args:
        file_or_flag (file or bool):
            either a configuration file pointed by a :ref:`file object
            <python:bltin-file-objects>` instance or something that evaluates
            to :class:`bool`.
    """
    if isinstance(file_or_flag, bool):
        if file_or_flag:
            logging.basicConfig(level=logging.DEBUG)
        else:
            # at least configure warnings
            logging.basicConfig(level=logging.WARNING)
    else:
        try:
            logging.config.fileConfig(file_or_flag)
        except Exception as error:
            # at least configure warnings
            logging.basicConfig(level=logging.WARNING)
            _logger.warning('Unable to configure logging from file %s. '
                            'Falling back to warning level.',
                            file_or_flag,
                            exc_info=True)


def main(args=None):
    """Run the daemon."""
    args = parse_arguments(args)

    configure_logging(args.logging)

    config = parse_config(args.config_file)

    checks = set_up_checks(config, 'check', Activity, error_none=True)
    wakeups = set_up_checks(config, 'wakeup', Wakeup)

    processor = Processor(
        checks, wakeups,
        config.getfloat('general', 'idle_time', fallback=300),
        config.getfloat('general', 'min_sleep_time', fallback=1200),
        config.getfloat('general', 'wakeup_delta', fallback=30),
        functools.partial(notify_and_suspend,
                          config.get('general', 'suspend_cmd'),
                          config.get('general', 'notify_cmd_wakeup',
                                     fallback=None),
                          config.get('general', 'notify_cmd_no_wakeup',
                                     fallback=None)),
        functools.partial(schedule_wakeup,
                          config.get('general', 'wakeup_cmd')),
        all_activities=args.all_checks)
    loop(processor,
         config.getfloat('general', 'interval', fallback=60),
         run_for=args.run_for,
         woke_up_file=config.get('general', 'woke_up_file',
                                 fallback='/var/run/autosuspend-just-woke-up'))


if __name__ == "__main__":
    main()
