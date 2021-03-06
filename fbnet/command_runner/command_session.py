#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import abc
import asyncio
import logging
import re
import time
from collections import namedtuple
from functools import wraps
from typing import Dict, Hashable, NamedTuple, Optional, Union

import asyncssh
from fbnet.command_runner_asyncio.CommandRunner import ttypes
from fbnet.command_runner_asyncio.CommandRunner.ttypes import SessionException

from .base_service import PeriodicServiceTask, ServiceObj
from .options import Option


# Register additional key exchange algorithms
asyncssh.public_key.register_public_key_alg(b"rsa-sha2-256", asyncssh.rsa._RSAKey)

log = logging.getLogger("fcr.CommandSession")

ResponseMatch = namedtuple("ResponseMatch", ["data", "matched", "groupdict", "match"])


class PeerInfo(NamedTuple):
    ip: Optional[str] = None
    port: Optional[Union[int, str]] = None

    def __str__(self) -> str:
        return f"({self.ip}, {self.port})"


class LogAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        return "[session_id=%s]: %s" % (self.extra["session"].id, msg), kwargs


class SessionReaperTask(PeriodicServiceTask):
    SESSION_REAP_PERIOD_S = Option(
        "--session_reap_period",
        help="Interval (in seconds) to cleanup stale or long-idle sessions "
        "(default: %(default)s)",
        type=int,
        default=60,
    )

    MAX_SESSION_IDLE_TIMEOUT_S = Option(
        "--max_session_idle_timeout",
        help="Maximal accepted value (in seconds) for session idle timeout "
        "(default: %(default)s)",
        type=int,
        default=30 * 60,
    )

    MAX_SESSION_LAST_ACCESS_TIMEOUT_S = Option(
        "--max_session_last_access_timeout",
        help="Max time a session can live since last access" "(default: %(default)s)",
        type=int,
        default=60 * 60,
    )

    COUNTER_KEY_REAPED_ALL = "session_reaper.reaped.all"

    def __init__(
        self,
        service: ServiceObj,
        # pyre-fixme[9]: sessions is `Dict[Hashable, CommandSession]` but is `None`
        sessions: Dict[Hashable, "CommandSession"] = None,
    ) -> None:
        super().__init__(
            service, name=self.__class__.__name__, period=self.SESSION_REAP_PERIOD_S
        )
        self._sessions = sessions or CommandSession._ALL_SESSIONS

    @classmethod
    def register_counters(cls, counter_mgr):
        counter_mgr.add_stats_counter(cls.COUNTER_KEY_REAPED_ALL, ["count"])

    def _bump_counters_for_reaped_session(self, session: "CommandSession") -> None:
        self.inc_counter(self.COUNTER_KEY_REAPED_ALL)

    async def run(self) -> None:
        """
        A session is accessed when a command begins executing, and is accessed
        again at the end of execution when it is released. A session is freed if
        1) it's idle for 'idle_timeout' sec after the last command execution;
        OR 2) it exceeds the max session time out since last accessed (this could
        happend when a command get stuck). This would prevent the thrift service
        from holding up open/stale connections to network devices.
        """
        try:
            self.logger.info(
                f"Session reaper woke up: curr_time={time.time()}, "
                f"session_count={len(self._sessions)}"
            )
            for key in list(self._sessions.keys()):
                if key not in self._sessions:
                    # Since this is an async method, it's possible that the session
                    # is closed before being reaped
                    continue
                session = self._sessions[key]
                curr_time = time.time()
                time_since_last_access = curr_time - session.last_access_time
                idle_timeout = min(
                    session.idle_timeout, self.MAX_SESSION_IDLE_TIMEOUT_S
                )
                if time_since_last_access > self.MAX_SESSION_LAST_ACCESS_TIMEOUT_S or (
                    not session.in_use and time_since_last_access > idle_timeout
                ):
                    self.logger.info(
                        f"Reap session {key}, "
                        f"last_access_time={session.last_access_time}, "
                        f"curr_time={curr_time}"
                    )
                    await session.close()
                    if key in self._sessions:
                        del self._sessions[key]
                    self._bump_counters_for_reaped_session(session)
            self.logger.info(
                f"Session reaper finished: session_count={len(self._sessions)}"
            )
        except Exception as ex:
            self.logger.exception(f"Error when reaping session {ex!r}")


def _update_last_access_time_and_in_use(fn):
    """
    This is a decorator to update the last access time of the session before and
    after calling the wrapped function
    NOTE: This is for internal use only within CommandSession
    """

    @wraps(fn)
    async def wrapper(self, *args, **kwargs):
        self._in_use_count += 1
        self._last_access_time = time.time()
        try:
            return await fn(self, *args, **kwargs)
        finally:
            self._in_use_count -= 1
            self._last_access_time = time.time()

    return wrapper


class CommandSession(ServiceObj):
    """
    A session for running commands on devices. Before running a command a
    CommandSession needs to be created. The connection to the device is
    established asynchronously, The user should wait for the session to
    be connected before trying to send commands to the device.

    Once a session is established, a set of read and write streams will be
    associated with the session.
    """

    _ALL_SESSIONS: Dict[Hashable, "CommandSession"] = {}

    # the prompt is at the end of input. So rather then searching in the entire
    # buffer, we will only look in the trailing data
    _MAX_PROMPT_SIZE = 100

    def __init__(self, service, devinfo, options, loop):

        # Setup devinfo as this is needed to create the logger
        self._devinfo = devinfo

        super().__init__(service)

        self._opts = options

        device = self._opts.get("device")
        self._extra_options = (
            device and device.session_data and device.session_data.extra_options
        ) or {}

        self._hostname = devinfo.hostname

        self._extra_info = {}
        self._exit_status = None

        # use the specified username/password or fallback to device defaults
        self._username = options.get("username") or devinfo.username
        self._password = options.get("password") or devinfo.password
        self._client_ip = options["client_ip"]
        self._client_port = options["client_port"]
        self._loop = loop

        # TODO: remove _cmd_stream from the base class CommandSession (some
        # session type, e.g., rpc base session, does not need this property)
        self._cmd_stream = None
        self._connected = False
        self._event = asyncio.Condition(loop=self._loop)

        self.logger.info("Created key=%s", self.key)
        # Record the session in the cache
        self._ALL_SESSIONS[self.key] = self

        self._last_access_time: float = time.time()
        self._in_use_count: int = 0

    def get_session_name(self):
        return self.objname

    def get_peer_info(self):
        return self._extra_info.get("peer")

    def create_logger(self):
        logger = logging.getLogger(
            "fcr.{klass}.{dev.vendor_name}.{dev.hostname}".format(
                klass=self.__class__.__name__, dev=self._devinfo
            )
        )

        return LogAdapter(logger, {"session": self})

    def build_result(self, output, status, command):
        return ttypes.CommandResult(output=output, status=status, command=command)

    def __repr__(self):
        return "%s [%s] [%s]" % (
            self.__class__.__name__,
            self._devinfo.hostname,
            self.id,
        )

    @classmethod
    def register_counters(cls, counters):
        counters.register_counter("%s.setup" % cls.__name__)
        counters.register_counter("%s.connected" % cls.__name__)
        counters.register_counter("%s.failed" % cls.__name__)
        counters.register_counter("%s.closed" % cls.__name__)

    @classmethod
    def get_session_count(cls):
        return len(cls._ALL_SESSIONS)

    @classmethod
    async def wait_sessions(cls, req_name, service):
        session_count = cls.get_session_count()

        while session_count != 0:
            await asyncio.sleep(1, loop=service.loop)
            session_count = cls.get_session_count()
            service.logger.info("%s: pending sessions: %d", req_name, session_count)

        service.logger.info("%s: no pending sesison", req_name)

    async def __aenter__(self):
        try:
            await self.setup()
        except Exception as e:
            await self.close()
            raise self._build_session_exc(e) from e

        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
        if exc_val:
            raise self._build_session_exc(exc_val) from exc_val

    def _build_session_exc(self, exc):
        msg = "Failed (session: %s, peer: %s): %r" % (
            self.get_session_name(),
            self.get_peer_info(),
            exc,
        )
        return SessionException(message=msg)

    @classmethod
    def get(cls, session_id, client_ip, client_port):
        key = (session_id, client_ip, client_port)
        try:
            return cls._ALL_SESSIONS[key]
        except KeyError as ke:
            raise KeyError("Session not found", key) from ke

    @property
    def hostname(self):
        return self._hostname

    @property
    def username(self):
        return self._username

    @property
    def devinfo(self):
        return self._devinfo

    @property
    def id(self):
        return id(self)

    @property
    def key(self):
        return (self.id, self._client_ip, self._client_port)

    @property
    def open_timeout(self):
        return self._opts.get("open_timeout")

    @property
    def use_mgmt_ip(self):
        return self._opts.get("mgmt_ip")

    @property
    def idle_timeout(self):
        return self._opts.get("idle_timeout")

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_access_time(self) -> float:
        return self._last_access_time

    @property
    def in_use(self) -> bool:
        return self._in_use_count > 0

    @property
    def exit_status(self):
        return self._exit_status

    async def _create_connection(self):
        await self.connect()

    @_update_last_access_time_and_in_use
    async def setup(self):
        self.inc_counter("%s.setup" % self.objname)
        try:
            await asyncio.wait_for(
                self._create_connection(), self.open_timeout, loop=self._loop
            )
        except asyncio.TimeoutError:
            self.logger.error("Timeout during connection setup")
            data = []
            # TODO(mzheng): Move the _steam_reader check to subclasses that
            # define it
            if hasattr(self, "_stream_reader") and self._stream_reader:
                data = await self._stream_reader.drain()
            raise asyncio.TimeoutError(
                "Timeout during connection setup. Currently received data "
                f"(last 200 char): {data[-200:]}"
            )
        return self

    async def connect(self):
        """
        Initiates a connection on the session
        """
        try:
            self._cmd_stream = await self._connect()
            self.inc_counter("%s.connected" % self.objname)
            self.logger.info("Connected: %s", self._extra_info)
        except Exception as e:
            self.logger.error("Connect Failed %r", e)
            self.inc_counter("%s.failed" % self.objname)
            raise e

    async def close(self):
        """
        Close the session. This removes the session from the cache. Also
        invokes the session specific _close method
        """
        try:
            self.logger.debug("Closing session")
            if self.key in self._ALL_SESSIONS:
                del self._ALL_SESSIONS[self.key]
        finally:
            self.inc_counter("%s.closed" % self.objname)
            await self._close()
            if self._cmd_stream is not None:
                self._cmd_stream.close()
            self._connected = False

    @_update_last_access_time_and_in_use
    async def run_command(self, command, *args, **kwargs):
        return await self._run_command(command, *args, **kwargs)

    @abc.abstractmethod
    async def _connect(self):
        """
        This needs to be implemented by the actual session classes
        """
        pass

    @abc.abstractmethod
    async def _close(self):
        """
        This needs to be implemented by the actual session classes
        """
        pass

    @abc.abstractmethod
    async def _run_command(self, command, *args, **kwargs):
        """
        This needs to be implemented by the actual session classes
        """
        pass

    async def wait_until_connected(self, timeout=None):
        """
        Wait until the session is marked as connected
        """
        await self.wait_for(lambda _: self._connected, timeout=timeout)

    async def _notify(self):
        """
        notify a change in stream state
        """
        await self._event.acquire()
        self._event.notify_all()
        self._event.release()

    async def wait_for(self, predicate, timeout=None):
        """
        Wait for condition to become true on the session
        """
        await self._event.acquire()
        await asyncio.wait_for(
            self._event.wait_for(lambda: predicate(self)),
            timeout=timeout,
            loop=self._loop,
        )
        self._event.release()


class CommandStreamReader(asyncio.StreamReader):
    """
    A Reader for commmand responses

    Extends the asyncio.StreamReader and adds support for waiting for regex
    match on received data
    """

    QUICK_COMMAND_RUNTIME = 1
    COMMAND_DATA_TIMEOUT = 1

    def __init__(self, session, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._session = session

    @property
    def logger(self):
        return self._session.logger

    async def wait_for(self, predicate):
        """
        Wait for the predicate to become true on the stream. As and when new
        data is available, the predicate will be re-evaluated.
        """

        if self._exception is not None:
            raise self._exception

        res = predicate(self._buffer)

        start_ts = time.time()

        while res is None:
            self.logger.debug(
                "match failed in: %d: %d: %s",
                len(self._buffer),
                self._limit,
                self._buffer[-100:],
            )
            self._session.inc_counter("streamreader.wait_for_retry")

            if len(self._buffer) > self._limit:
                self._session.inc_counter("streamreader.overrun")
                raise RuntimeError(
                    "Reader buffer overrun: %d: %d" % (len(self._buffer), self._limit)
                )

            now = time.time()

            if now - start_ts > self.QUICK_COMMAND_RUNTIME:
                # Keep waiting for data till we get a timeout
                try:
                    while True:
                        fut = self._wait_for_data("CommandStreamReader.wait_for")
                        await asyncio.wait_for(
                            fut, timeout=self.COMMAND_DATA_TIMEOUT, loop=self._loop
                        )
                except asyncio.TimeoutError:
                    # Now try to match the prompt
                    pass
            else:
                # match quickly initially
                await self._wait_for_data("CommandStreamReader.wait_for")

            res = predicate(self._buffer)

        self.logger.debug("match found at: %s", res)

        return res

    def _search_re(self, regex, data, start=0):
        self.logger.debug("searching for: %s", regex)
        return regex.search(data, start)

    async def readuntil_re(self, regex, start=0):
        """
        Read data until a regex is matched on the input stream
        """
        self.logger.debug("readuntil_re: %s", regex)

        try:
            match = await self.wait_for(lambda data: regex.search(data, start))

            m_beg, m_end = match.span()
            # We are matching against the data stored stored in bytebuffer
            # The bytebuffer is manipulated in place. After we read the data
            # the buffer may get overwritten. The match object seems to be
            # directly referring the data in bytebuffer. This causes a problem
            # when we try to find the matched groups in match object.
            #
            # In [38]: data = bytearray(b"localhost login:")
            #
            # In [39]: rex = re.compile(b'(?P<login>.*((?<!Last ).ogin|.sername):)|(?P<passwd>\n.*assword:)|(?P<prompt>\n.*[%#>])|(?P<ignore>( to cli \\])|(who is on this device.\\]\r\n)|(Press R
            #     ...: ETURN to get started\r\n))\\s*$')
            #
            # In [40]: m = rex.search(data)
            #
            # In [41]: m.groupdict()
            # Out[41]: {'ignore': None, 'login': b'localhost login:', 'passwd': None, 'prompt': None}
            #
            # In [42]: data[:]=b'overwrite'
            #
            # In [43]: m.groupdict()
            # Out[43]: {'ignore': None, 'login': b'overwrite', 'passwd': None, 'prompt': None}
            #
            groupdict = match.groupdict()
            rdata = await self.read(m_end)
            data = rdata[:m_beg]  # Data before the regex match
            matched = rdata[m_beg:m_end]  # portion that matched regex
        except AssertionError:
            if self._eof:
                # We are at the EOF. Read the whole buffer and send it back
                data = await self.read(len(self._buffer))
                matched = b""
                match = None
                groupdict = None
            else:
                # re-raise the exception
                raise

        return ResponseMatch(data, matched, groupdict, match)

    async def drain(self):
        """
        Drain the read buffer. Typically used before sending a new commands to
        make sure the stream in in sane state
        """
        return await self.read(len(self._buffer))


class CommandStream(asyncio.StreamReaderProtocol):

    # TODO: make this tweakable from configerator
    _BUFFER_LIMIT = 100 * (2 ** 20)  # 100M

    def __init__(self, session, loop):
        super().__init__(
            CommandStreamReader(session, limit=self._BUFFER_LIMIT, loop=loop),
            client_connected_cb=self._on_connect,
            loop=loop,
        )
        self._session = session
        self._loop = loop

    def _on_connect(self, stream_reader, stream_writer):
        """
        called when transport is connected
        """
        # Sometimes the remote side doesn't send the newline for the first
        # prompt. This causes our prompt matching to fail. Here we inject a
        # newline to normalize these cases. This keeps our prompt processing
        # simple.
        super().data_received(b"\n")
        self._session._session_connected(stream_reader, stream_writer)

    def close(self):
        if self._stream_writer:
            self._stream_writer.close()

    def data_received(self, data, datatype=None):
        # TODO: check if we need to handle stderr data separately
        # for stderr data: datatype == EXTENDED_DATA_STDERR
        return super().data_received(data)

    def session_started(self):
        # Not used yet. But needs to be defined
        pass

    def exit_status_received(self, status):
        self._session.exit_status_received(status)


class CliCommandSession(CommandSession):
    """
    A command session for CLI commands. Does prompt processing on the command stream.
    """

    def __init__(self, service, devinfo, options, loop):
        super().__init__(service, devinfo, options, loop)

        self._cmd_stream = None
        self._stream_reader = None  # for reading data from device
        self._stream_writer = None  # for writing data to the device
        # TODO: investigate if we need an error stream

    @classmethod
    def register_counters(cls, counters):
        super().register_counters(counters)
        counters.register_counter("streamreader.wait_for_retry")
        counters.register_counter("streamreader.overrun")
        counters.register_counter("streamreader.overrun")

    async def _setup_connection(self):
        # At this point login process should already be complete. If a
        # particular session needs to send password, it should override this
        # method and complete the login before calling this method
        await self.wait_prompt()
        for cmd in self._devinfo.vendor_data.cli_setup:
            await self.run_command(cmd + b"\n")

    async def _create_connection(self):
        await super()._create_connection()
        await self.wait_until_connected(self.open_timeout)
        await self._setup_connection()

    async def wait_prompt(self, prompt_re=None):
        """
        Wait for a prompt
        """
        return await self._stream_reader.readuntil_re(
            prompt_re or self._devinfo.prompt_re, -self._MAX_PROMPT_SIZE
        )

    async def _wait_response(self, cmd, prompt_re):
        """
        Wait for command response from the device
        """
        self.logger.debug("Waiting for prompt")
        resp = await self.wait_prompt(prompt_re)
        return resp

    def _fixup_whitespace(self, output):
        # we need to sanitize the output to remove '\r' and other chars.
        # List of chars that will be removed
        #        ' *\x08+': space* followed by backspace characters
        #          '\x07' : BEL(bell) char
        output = re.sub(br".\x08|\x07", b"", output)

        #
        # We need to apply following transforms
        #   '\r+\n' -> '\n'
        #   '\n\r+' -> '\n'
        #   '\r' -> '\n'     standalone \r
        output = re.sub(br"(\r+\n)|(\n\r+)|\r", b"\n", output)

        return output.strip()

    def _format_output(self, cmd, resp):
        """
        Format the output to comply with following format

            <prompt> <command>
            command-output
            ...

        In addition '\r\n' | '\n\r' | '\r' will be replace with '\n'

        """
        cmd_words = cmd.split()

        # Fixup the white spaces first, as some devices are inserting backspace
        # characters in the command echo
        cmd_output = self._fixup_whitespace(resp.data)

        # Command regex in the output
        # [SPACE]{Command string}[SPACE]
        # The words in the command string can be separated by mulitple spaces.
        # for e.g regex for matching 'show version' command would be
        #    b'^\s*show\s+version\s*$'
        # We also need to escape the words to handle characters like '|'
        cmd_words_esc = (re.escape(w) for w in cmd_words)
        cmd_re = br"^\s*" + br"\s+".join(cmd_words_esc) + br"([ \t]*\n)*"

        # Now replace the 'command string' in the output with a sanitized
        # version (redundant spaces removed)
        # '  show  version  '  ==>  'show version'
        cmd_output = re.sub(cmd_re, b" ".join(cmd_words) + b"\n", cmd_output, 1, re.M)

        # Now we need to prepend the prompt to the command output. The prompt is
        # the matched part in the 'resp'
        output = resp.matched.strip() + b" " + cmd_output

        return output

    async def _run_command(self, cmd, timeout=None, prompt_re=None):
        """
        Run a command and return response to user
        """
        if not self._connected:
            raise RuntimeError(
                "Not Connected", "status: %r" % self.exit_status, self.key
            )

        # Ideally there should be no data on the stream. We will in any case
        # drain any stale data. This is mostly for debugging and making sure
        # that we are in sane state
        stale_data = await self._stream_reader.drain()
        if len(stale_data) != 0:
            self.logger.warning("Stale data on session: %s", stale_data)

        output = []

        commands = cmd.splitlines()
        for command in commands:
            cmdinfo = self._devinfo.get_command_info(
                command, self._opts.get("command_prompts")
            )

            self.logger.info("RUN: %r", cmdinfo.cmd)

            # Send any precmd data (e.g. \x15 to clear the commandline)
            if cmdinfo.precmd:
                self._stream_writer.write(cmdinfo.precmd)

            self._stream_writer.write(cmdinfo.cmd)

            try:
                prompt = prompt_re or cmdinfo.prompt_re

                resp = await asyncio.wait_for(
                    self._wait_response(command, prompt),
                    timeout or self._devinfo.vendor_data.cmd_timeout_sec,
                    loop=self._loop,
                )
                output.append(self._format_output(command, resp))
            except asyncio.TimeoutError:
                self.logger.error("Timeout waiting for command response")
                data = await self._stream_reader.drain()
                raise RuntimeError("Command Response Timeout", data[-200:])

        return b"\n".join(output).rstrip()

    def _session_connected(self, stream_reader, stream_writer):
        """
        This called once the session is connected to the transport.
        stream_reader and stream_writer are used for receiving and sending
        data on the session
        """
        self._stream_reader = stream_reader
        self._stream_writer = stream_writer
        self._connected = True

        # Notify anyone waiting for session to be connected
        asyncio.ensure_future(self._notify(), loop=self._loop)

    def exit_status_received(self, status):
        self.logger.info("exit status received: %s", status)
        self._connected = False
        self._exit_status = status


# pyre-fixme[11]: Annotation `asyncssh.SSHClient` is not defined as a type.
class SSHCommandClient(asyncssh.SSHClient):
    """
    The connection objects are leaked if the session timeout while the
    authentication is in progres. The fix ideally needs to be implemented in
    asyncssh. For now we are adding a workaround in FCR. We will save the
    connection object when we get a connection_made callback. This will be used
    to close the connection when we close the session.
    """

    def __init__(self, session):
        super().__init__()
        self._session = session

    def connection_made(self, conn):
        super().connection_made(conn)
        self._session.connection_made(conn)


class SSHCommandSession(CliCommandSession):
    TERM_TYPE = "vt100"

    def __init__(self, counter_mgr, devinfo, options, loop):
        super().__init__(counter_mgr, devinfo, options, loop)

        self._conn = None
        self._chan = None

    def connection_made(self, conn):
        s = conn.get_extra_info("socket")
        self._extra_info["fd"] = s.fileno()
        self._extra_info["sockname"] = conn.get_extra_info("sockname")
        self._conn = conn

    def _client_factory(self):
        return SSHCommandClient(self)

    async def dest_info(self):
        ip = self._devinfo.get_ip(self._opts)
        port = int(
            self._extra_options.get("port") or self._devinfo.vendor_data.get_port()
        )
        return (ip, port, self._username, self._password)

    async def _connect(self, subsystem=None, exec_command=None):
        """
        Some session types require us to run a command to start a session. The
        SSH protocol defines three ways to start a session.
        1. shell: this starts a regular user shell on the remote system. This is
                  the most common way of using SSH. If none of 'subsystem' or
                  'command' is specified, this is the method that we use.

        2. exec: Here we specify the command we want to run on remote system.
                 This allows the user start a custom shell. For example run
                 a 'netconf' command to start netconf session

        3. subsystem: Here instead of running a comman we specify a subsystems
                      that has been configured on the remote system. These are
                      predefined systems

        see sec 6.5 https://tools.ietf.org/html/rfc4254 for more details
        """
        ip, port, user, passwd = await self.dest_info()
        self._extra_info["peer"] = PeerInfo(ip, port)

        if self._devinfo.proxy_required(ip):
            host = self.service.get_http_proxy_url(ip)
        elif self._devinfo.should_nat(ip):
            host = await self._devinfo.translate_address(ip)
        else:
            host = ip

        self.logger.info("Connecting to: %s: %d", host, port)

        # known_hosts is set to None to disable the host verifications. Without
        # this the connection setup fails for some devices
        conn, _ = await asyncssh.create_connection(
            self._client_factory,
            host=host,
            port=port,
            username=user,
            password=passwd,
            client_keys=None,
            known_hosts=None,
        )

        chan, cmd_stream = await self._conn.create_session(
            lambda: CommandStream(self, self._loop),
            encoding=None,
            term_type=self.TERM_TYPE,
            subsystem=subsystem,
            command=exec_command,
        )
        self._chan = chan
        return cmd_stream

    async def _close(self):
        if self._chan is not None:
            self._chan.close()
        if self._conn is not None:
            self._conn.close()
