# Copyright (C) 1999--2002  Joel Rosdahl
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307  USA
#
# keltus <keltus@users.sourceforge.net>
#
# $Id: irclib.py,v 1.47 2008/09/25 22:00:59 keltus Exp $

"""irclib -- Internet Relay Chat (IRC) protocol client library.

This library is intended to encapsulate the IRC protocol at a quite
low level.  It provides an event-driven IRC client framework.  It has
a fairly thorough support for the basic IRC protocol, CTCP, DCC chat,
but DCC file transfers is not yet supported.

In order to understand how to make an IRC client, I'm afraid you more
or less must understand the IRC specifications.  They are available
here: [IRC specifications].

The main features of the IRC client framework are:

  * Abstraction of the IRC protocol.
  * Handles multiple simultaneous IRC server connections.
  * Handles server PONGing transparently.
  * Messages to the IRC server are done by calling methods on an IRC
    connection object.
  * Messages from an IRC server triggers events, which can be caught
    by event handlers.
  * Reading from and writing to IRC server sockets are normally done
    by an internal select() loop, but the select()ing may be done by
    an external main loop.
  * Functions can be registered to execute at specified times by the
    event-loop.
  * Decodes CTCP tagging correctly (hopefully); I haven't seen any
    other IRC client implementation that handles the CTCP
    specification subtilties.
  * A kind of simple, single-server, object-oriented IRC client class
    that dispatches events to instance methods is included.

Current limitations:

  * The IRC protocol shines through the abstraction a bit too much.
  * Data is not written asynchronously to the server, i.e. the write()
    may block if the TCP buffers are stuffed.
  * There are no support for DCC file transfers.
  * The author haven't even read RFC 2810, 2811, 2812 and 2813.
  * Like most projects, documentation is lacking...

.. [IRC specifications] http://www.irchelp.org/irchelp/rfc/
"""

import bisect
import re
import select
import socket
import string
import sys
import time
import types
import threading
import traceback
import math

import say_levels

VERSION = 0, 4, 8
DEBUG = 0

# TODO
# ----
# (maybe) thread safety
# (maybe) color parser convenience functions
# documentation (including all event types)
# (maybe) add awareness of different types of ircds
# send data asynchronously to the server (and DCC connections)
# (maybe) automatically close unused, passive DCC connections after a while

# NOTES
# -----
# connection.quit() only sends QUIT to the server.
# ERROR from the server triggers the error event and the disconnect event.
# dropping of the connection triggers the disconnect event.

strip_formatting_re = re.compile('(?:\x03[0-9]*(?:,[0-9]+)?|\x02|\x1f)')

class IRCError(Exception):
    """Represents an IRC exception."""
    pass


class IRC:
    """Class that handles one or several IRC server connections.

    When an IRC object has been instantiated, it can be used to create
    Connection objects that represent the IRC connections.  The
    responsibility of the IRC object is to provide an event-driven
    framework for the connections and to keep the connections alive.
    It runs a select loop to poll each connection's TCP socket and
    hands over the sockets with incoming data for processing by the
    corresponding connection.

    The methods of most interest for an IRC client writer are server,
    add_global_handler, remove_global_handler, execute_at,
    execute_delayed, process_once and process_forever.

    Here is an example:

        irc = irclib.IRC()
        server = irc.open_connection(\"irc.some.where\", 6667, \"my_nickname\")
        server.connect()
        server.privmsg(\"a_nickname\", \"Hi there!\")
        irc.process_forever()

    This will connect to the IRC server irc.some.where on port 6667
    using the nickname my_nickname and send the message \"Hi there!\"
    to the nickname a_nickname.
    """

    def __init__(self, fn_to_add_socket=None,
                 fn_to_remove_socket=None,
                 fn_to_add_timeout=None):
        """Constructor for IRC objects.

        Optional arguments are fn_to_add_socket, fn_to_remove_socket
        and fn_to_add_timeout.  The first two specify functions that
        will be called with a socket object as argument when the IRC
        object wants to be notified (or stop being notified) of data
        coming on a new socket.  When new data arrives, the method
        process_data should be called.  Similarly, fn_to_add_timeout
        is called with a number of seconds (a floating point number)
        as first argument when the IRC object wants to receive a
        notification (by calling the process_timeout method).  So, if
        e.g. the argument is 42.17, the object wants the
        process_timeout method to be called after 42 seconds and 170
        milliseconds.

        The three arguments mainly exist to be able to use an external
        main loop (for example Tkinter's or PyGTK's main app loop)
        instead of calling the process_forever method.

        An alternative is to just call ServerConnection.process_once()
        once in a while.
        """

        if fn_to_add_socket and fn_to_remove_socket:
            self.fn_to_add_socket = fn_to_add_socket
            self.fn_to_remove_socket = fn_to_remove_socket
        else:
            self.fn_to_add_socket = None
            self.fn_to_remove_socket = None

        self.fn_to_add_timeout = fn_to_add_timeout
        self.connections = []
        self.handlers = {}
        self.delayed_commands = [] # list of tuples in the format (time, function, arguments)
        self.charsets = {'': ['utf-8']}
        self.connection_intervals = {'': 1}
        self.connection_stacks = {}

        self.add_global_handler("ping", _ping_ponger, -42)


    def _connection_loop(self, server_str):
        stack = self.connection_stacks[server_str]
        if len(stack) > 0:
            stack[0][0](*stack[0][1])
            stack.pop(0)
            delay = self.connection_interval(server=server_str)
            self.bot.error(2, 'waiting '+str(delay)+' seconds before next connection on '+server_str, debug=True)
            self.execute_delayed(delay, self._connection_loop, (server_str,))
        else:
            self.connection_stacks.pop(server_str)


    def connection_interval(self, server='', seconds=None):
        if seconds:
            self.connection_intervals[server] = seconds
            return seconds
        elif self.connection_intervals.has_key(server):
            return self.connection_intervals[server]
        else:
            return self.connection_intervals['']


    def get_connection(self, server, port, nickname):
        for c in self.connections:
            if c.server == server and c.port == port and nickname in [c.nickname, c.real_nickname]:
                return c
        return None

    def has_connection(self, server, port, nickname):
        if self.get_connection(server, port, nickname):
            return True
        return False

    def open_connection(self, server, port, nickname, delay=None):
        """Creates or returns an existing ServerConnection object for nickname at server:port.

            server -- Server name.

            port -- Port number.

            nickname -- The nickname."""

        c = self.get_connection(server, port, nickname)
        if c:
            return c
        c = ServerConnection(self, server, port, nickname)
        server_str = c._server_str()
        if not self.connection_stacks.has_key(server_str):
            self.connection_stacks[server_str] = []
            delay = self.connection_interval(server=server_str, seconds=delay)
            self.execute_delayed(delay, self._connection_loop, (server_str,))
        self.connections.append(c)
        return c

    def process_data(self, sockets):
        """Called when there is more data to read on connection sockets.

        Arguments:

            sockets -- A list of socket objects.

        See documentation for IRC.__init__.
        """
        for s in sockets:
            for c in self.connections:
                if s == c._get_socket():
                    c.lock.acquire()
                    if hasattr(c, 'socket'):
                        c.process_data()
                    c.lock.release()

    def process_timeout(self):
        """Called when a timeout notification is due.

        See documentation for IRC.__init__.
        """
        t = time.time()
        while self.delayed_commands:
            if t >= self.delayed_commands[0][0]:
                self.delayed_commands[0][1](*self.delayed_commands[0][2])
                del self.delayed_commands[0]
            else:
                break

    def process_once(self, timeout=0):
        """Process data from connections once.

        Arguments:

            timeout -- How long the select() call should wait if no
                       data is available.

        This method should be called periodically to check and process
        incoming data, if there are any.  If that seems boring, look
        at the process_forever method.
        """
        sockets = map(lambda x: x._get_socket(), self.connections)
        sockets = filter(lambda x: x and not isinstance(x, basestring), sockets)
        if sockets:
            (i, o, e) = select.select(sockets, [], [], timeout)
            self.process_data(i)
        else:
            time.sleep(timeout)
        self.process_timeout()

    def process_forever(self, timeout=0.2):
        """Run an infinite loop, processing data from connections.

        This method repeatedly calls process_once.

        Arguments:

            timeout -- Parameter to pass to process_once.
        """
        while 1:
            if self.bot.halt:
                self.disconnect_all(message='Stopping bot')
                break
            try:
                self.process_once(timeout)
            except ServerNotConnectedError as e:
                if len(e.args) > 0:
                    c = e.args[0]
                else:
                    self.bot.error(say_levels.error, 'Unkonwn exception on IRC thread:\n'+str(e.args))
                    continue
                if c.nickname == self.bot.nickname:
                    self.bot.restart(message='Lost bot IRC connection')
                else:
                    c.disconnect(volontary=True)
                    c.connect()
            except:
                self.bot.error(say_levels.error, 'Unkonwn exception on IRC thread:\n'+traceback.format_exc(), send_to_admins=True)

    def disconnect_all(self, message="", volontary=True):
        """Disconnects all connections."""
        for c in self.connections:
            c.disconnect(message, volontary=volontary)

    def add_global_handler(self, event, handler, priority=0):
        """Adds a global handler function for a specific event type.

        Arguments:

            event -- Event type (a string).  Check the values of the
            numeric_events dictionary in irclib.py for possible event
            types.

            handler -- Callback function.

            priority -- A number (the lower number, the higher priority).

        The handler function is called whenever the specified event is
        triggered in any of the connections.  See documentation for
        the Event class.

        The handler functions are called in priority order (lowest
        number is highest priority).  If a handler function returns
        \"NO MORE\", no more handlers will be called.
        """
        if not event in self.handlers:
            self.handlers[event] = []
        bisect.insort(self.handlers[event], ((priority, handler)))

    def remove_global_handler(self, event, handler):
        """Removes a global handler function.

        Arguments:

            event -- Event type (a string).

            handler -- Callback function.

        Returns 1 on success, otherwise 0.
        """
        if not event in self.handlers:
            return 0
        for h in self.handlers[event]:
            if handler == h[1]:
                self.handlers[event].remove(h)
        return 1

    def execute_at(self, at, function, arguments=()):
        """Execute a function at a specified time.

        Arguments:

            at -- Execute at this time (standard \"time_t\" time).

            function -- Function to call.

            arguments -- Arguments to give the function.
        """
        self.execute_delayed(at-time.time(), function, arguments)

    def execute_delayed(self, delay, function, arguments=()):
        """Execute a function after a specified time.

        Arguments:

            delay -- How many seconds to wait.

            function -- Function to call.

            arguments -- Arguments to give the function.
        """
        bisect.insort(self.delayed_commands, (delay+time.time(), function, arguments))
        if self.fn_to_add_timeout:
            self.fn_to_add_timeout(delay)

    def dcc(self, dcctype="chat"):
        """Creates and returns a DCCConnection object.

        Arguments:

            dcctype -- "chat" for DCC CHAT connections or "raw" for
                       DCC SEND (or other DCC types). If "chat",
                       incoming data will be split in newline-separated
                       chunks. If "raw", incoming data is not touched.
        """
        c = DCCConnection(self, dcctype)
        self.connections.append(c)
        return c

    def _handle_event(self, connection, event):
        """[Internal]"""
        h = self.handlers
        for handler in h.get("all_events", []) + h.get(event.eventtype(), []):
            if handler[1](connection, event) == "NO MORE":
                return

    def _remove_connection(self, connection):
        """[Internal]"""
        if connection in self.connections:
            self.connections.remove(connection)
        if self.fn_to_remove_socket:
            self.fn_to_remove_socket(connection._get_socket())


class UnknownChannel(IRCError): pass

LEFT, LEAVING, NOT_IN, JOINING, JOINED = range(5)

class Channel:

    def __init__(self, connection, channel_name):
        self.connection = connection
        self.channel_name = channel_name
        self.callbacks = []
        self.state = NOT_IN

    def _callback(self, error):
        if not error:
            self.state = JOINED
        else:
            self.state = NOT_IN
        m = 'channel "'+self.channel_name+'" on connection "'+str(self.connection)+'"'
        if len(self.callbacks) == 0:
            self.connection.irclibobj.bot.error(1, 'no join callback for '+m, debug=True)
        else:
            self.connection.irclibobj.bot.error(1, 'calling '+str(len(self.callbacks))+' join callback(s) for '+m, debug=True)
            for f in self.callbacks:
                f(self.channel_name, error)

    def add_callback(self, callback):
        if callback and not callback in self.callbacks:
            self.callbacks.append(callback)

    def join(self, key=None, callback=None):
        self.state = JOINING
        self.key = key
        self.add_callback(callback)
        self.connection.send_raw("JOIN %s%s" % (self.channel_name, (key and (" " + key))))

    def part(self, message=None):
        if self.state <= NOT_IN:
            return
        self.state = LEAVING
        self.connection.send_raw("PART " + self.channel_name + (message and (" " + message)))

    def rejoin(self):
        self.join(key=self.key)

    def remove_callback(self, callback):
        try:
            self.callbacks.remove(callback)
        except ValueError:
            pass


_rfc_1459_command_regexp = re.compile("^(:(?P<prefix>[^ ]+) +)?(?P<command>[^ ]+)( *(?P<argument> .+))?")

class Connection:
    """Base class for IRC connections.

    Must be overridden.
    """
    def __init__(self, irclibobj):
        self.irclibobj = irclibobj

    def _get_socket():
        raise IRCError, "Not overridden"

    ##############################
    ### Convenience wrappers.

    def execute_at(self, at, function, arguments=()):
        self.irclibobj.execute_at(at, function, arguments)

    def execute_delayed(self, delay, function, arguments=()):
        self.irclibobj.execute_delayed(delay, function, arguments)


class ServerConnectionError(IRCError):
    pass

class ServerNotConnectedError(ServerConnectionError):
    pass


# Huh!?  Crrrrazy EFNet doesn't follow the RFC: their ircd seems to
# use \n as message separator!  :P
_linesep_regexp = re.compile("\r?\n")

class ServerConnection(Connection):
    """This class represents an IRC server connection.

    ServerConnection objects are instantiated by calling the server
    method on an IRC object.
    """

    def __init__(self, irclibobj, server, port, nickname):
        Connection.__init__(self, irclibobj)
        self.connected = False  # Not connected yet.
        self.logged_in = False
        self.used_by = 0
        self.socket = None
        self.ssl = None
        self.server = server
        self.port = port
        self.nickname = nickname
        self.nick_callbacks = []
        self.join_callbacks = {}
        self.lock = threading.RLock()
        self.channels = {}
        self.irc_id = None
        self.previous_buffer = ""
        self.handlers = {}
        self.real_server_name = ""
        self.new_nickname = None


    def __str__(self):
        return self.real_nickname+' at '+self._server_str()


    def _decode(self, bytes):
        charsets = self.irclibobj.charsets[self._server_str()] or self.irclibobj.charsets['']
        for codec in charsets:
            try:
                return bytes.decode(codec)
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
        raise Exception, 'no suitable codec found for: '+repr(bytes)+'\ntried: '+' '.join(charsets)


    def _ping(self):
        self.irclibobj.execute_delayed(60, self._ping)
        if self.connected == False:
            return
        self.irclibobj.bot.error(1, 'sending IRC ping', debug=True)
        self.ping(self.get_server_name())


    def _server_str(self):
        return self.server+':'+str(self.port)


    def connect(self, password=None, username=None,
                ircname=None, localaddress="", localport=0, ssl=False, ipv6=False, nick_callback=None, charsets=None):
        """Connect to the server.

        Arguments:

            password -- Password (if any).

            username -- The username.

            ircname -- The IRC name ("realname").

            localaddress -- Bind the connection to a specific local IP address.

            localport -- Bind the connection to a specific local port.

            ssl -- Enable support for ssl.

            ipv6 -- Enable support for ipv6.

        This function can be called to reconnect a closed connection.

        Returns the ServerConnection object.
        """
        
        self.lock.acquire()
        
        if nick_callback:
            self.add_nick_callback(nick_callback)
        
        if self.used_by > 0:
            self.used_by += 1
            self.irclibobj.bot.error(3, 'using existing IRC connection for '+self.__str__()+', this connection is now used by '+str(self.used_by)+' bridges', debug=True)
            if self.logged_in:
                self._call_nick_callbacks(None)
            self.lock.release()
            return self

        if self.socket != 'closed':
            self.used_by = 1
            if charsets or not self.irclibobj.charsets.has_key(self._server_str()):
                self.irclibobj.charsets[self._server_str()] = charsets
            self.real_nickname = self.nickname
            self.username = username or self.nickname
            self.ircname = ircname or self.nickname
            self.password = password
            self.localaddress = localaddress
            self.localport = localport
            self.localhost = socket.gethostname()
            self.ssl = ssl
            self.ipv6 = ipv6

        self.irclibobj.connection_stacks[self._server_str()].append( (self._connect, ()) )
        
        self.lock.release()
        return self


    def _connect(self):
        
        self._ping()

        self.lock.acquire()

        if self.socket != 'closed':
            self.irclibobj.bot.error(3, 'opening new IRC connection for '+self.__str__(), debug=True)
        else:
            self.irclibobj.bot.error(3, 'reopening IRC connection for '+self.__str__(), debug=True)

        if self.ipv6:
            self.socket = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        else:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.socket.bind((self.localaddress, self.localport))
            self.socket.connect((self.server, self.port))
            if self.ssl:
                self.ssl = socket.ssl(self.socket)
        except socket.error, x:
            self.socket.close()
            self.socket = 'closed'
            raise ServerConnectionError, "Couldn't connect to socket: %s" % x
        self.connected = True
        if self.irclibobj.fn_to_add_socket:
            self.irclibobj.fn_to_add_socket(self.socket)

        # Log on...
        if self.password:
            self.pass_(self.password)
        if self.nick(self.nickname):
            self.user(self.username, self.ircname)
        
        # Rejoin channels
        if len(self.channels) > 0:
            for channel in self.channels.itervalues():
                channel.rejoin()
        
        self.lock.release()
        return self


    def _call_nick_callbacks(self, error, arguments=[]):
        if len(self.nick_callbacks) == 0:
            self.irclibobj.bot.error(1, 'no nick callback for "'+self.__str__()+'"', debug=True)
        else:
            self.irclibobj.bot.error(1, 'calling '+str(len(self.nick_callbacks))+' nick callback(s) for "'+self.__str__()+'"', debug=True)
            for f in self.nick_callbacks:
                f(error, arguments=arguments)
        self.nick_callbacks = []


    def add_nick_callback(self, callback):
        self.nick_callbacks.append(callback)


    def add_join_callback(self, channel, callback):
        self.lock.acquire()
        if not self.join_callbacks.has_key(channel):
            self.join_callbacks[channel] = []
        self.join_callbacks[channel].append(callback)
        self.lock.release()


    def close(self, message, volontary=True):
        """Close the connection.

        This method closes the connection permanently; after it has
        been called, the object is unusable.
        """

        self.irclibobj._remove_connection(self)
        self.disconnect(message=message, volontary=volontary)

    def _get_socket(self):
        """[Internal]"""
        return self.socket

    def get_server_name(self):
        """Get the (real) server name.

        This method returns the (real) server name, or, more
        specifically, what the server calls itself.
        """

        if self.real_server_name:
            return self.real_server_name
        else:
            return ""

    def process_data(self):
        """[Internal]"""

        try:
            if self.ssl:
                new_data = self.ssl.read(2**14)
            elif self.socket and hasattr(self.socket, 'recv'):
                new_data = self.socket.recv(2**14)
            else:
                return
        except socket.error, x:
            # The server hung up.
            self.disconnect("Connection reset by peer")
            return
        if not new_data:
            # Read nothing: connection must be down.
            self.disconnect("Connection reset by peer")
            return

        lines = _linesep_regexp.split(self.previous_buffer + new_data)

        # Save the last, unfinished line.
        self.previous_buffer = lines.pop()

        for line in lines:
            if DEBUG:
                print "FROM SERVER:", line

            if not line:
                continue

            line = self._decode(line)

            prefix = None
            command = None
            arguments = None
            self._handle_event(Event("all_raw_messages",
                                     self.get_server_name(),
                                     None,
                                     [line]))

            m = _rfc_1459_command_regexp.match(line)
            if m.group("prefix"):
                prefix = m.group("prefix")
                if not self.real_server_name:
                    self.real_server_name = prefix

            if m.group("command"):
                command = m.group("command").lower()

            if m.group("argument"):
                a = m.group("argument").split(" :", 1)
                arguments = a[0].split()
                if len(a) == 2:
                    arguments.append(a[1])

            # Translate numerics into more readable strings.
            if command in numeric_events:
                command = numeric_events[command]

            if command in ["privmsg", "notice"]:
                target, message = arguments[0], arguments[1]
                messages = _ctcp_dequote(message)

                if command == "privmsg":
                    if is_channel(target):
                        command = "pubmsg"
                else:
                    if is_channel(target):
                        command = "pubnotice"
                    else:
                        command = "privnotice"

                for m in messages:
                    if type(m) is types.TupleType:
                        if command in ["privmsg", "pubmsg"]:
                            command = "ctcp"
                        else:
                            command = "ctcpreply"

                        m = list(m)
                        if DEBUG:
                            print "command: %s, source: %s, target: %s, arguments: %s" % (
                                command, prefix, target, m)

                        # Remove formatting
                        for i in range(len(m)):
                            m[i] = strip_formatting_re.sub('', m[i])

                        self._handle_event(Event(command, prefix, target, m))
                        if command == "ctcp" and m[0] == "ACTION":
                            self._handle_event(Event("action", prefix, target, m[1:]))
                    else:
                        if DEBUG:
                            print "command: %s, source: %s, target: %s, arguments: %s" % (
                                command, prefix, target, [m])
                        self._handle_event(Event(command, prefix, target, [strip_formatting_re.sub('', m)]))
            else:
                target = None

                if command == "quit":
                    arguments = [arguments[0]]
                elif command == "ping":
                    target = arguments[0]
                else:
                    target = arguments[0]
                    arguments = arguments[1:]

                if command == "mode":
                    if not is_channel(target):
                        command = "umode"

                if command in ["nick", "welcome"]:
                    self.logged_in = True
                    if self.new_nickname and isinstance(target, basestring):
                        self.real_nickname = target
                        if self.new_nickname != target:
                            if len(self.new_nickname) > len(target):
                                self._handle_event(Event('nicknametoolong', None, None, None))
                            else:
                                self._handle_event(Event('erroneusnickname', None, None, None))
                        else:
                            self._call_nick_callbacks(None)
                        self.new_nickname = None

                if command == "join":
                    if self.irc_id != prefix:
                        self.irc_id = prefix
                        if DEBUG:
                            print "irc_id: %s" % (prefix)
                    channel = target.lower()
                    if self.channels[channel].state != JOINED:
                        self.channels[channel]._callback(None)

                if command in ['inviteonlychan', 'bannedfromchan', 'channelisfull', 'badchannelkey']:
                    self.channels[arguments[0].lower()]._callback(command)

                if DEBUG:
                    print "command: %s, source: %s, target: %s, arguments: %s" % (
                        command, prefix, target, arguments)

                # Remove formatting
                for i in range(len(arguments)):
                    arguments[i] = strip_formatting_re.sub('', arguments[i])

                self._handle_event(Event(command, prefix, target, arguments))

    def _handle_event(self, event):
        """[Internal]"""
        self.irclibobj._handle_event(self, event)
        if event.eventtype() in ['disconnect', 'nicknameinuse', 'nickcollision', 'erroneusnickname', 'nicknametoolong']:
            self._call_nick_callbacks(event.eventtype(), arguments=[event])
        if event.eventtype() in self.handlers:
            for fn in self.handlers[event.eventtype()]:
                fn(self, event)

    def add_global_handler(self, *args):
        """Add global handler.

        See documentation for IRC.add_global_handler.
        """
        self.irclibobj.add_global_handler(*args)

    def remove_global_handler(self, *args):
        """Remove global handler.

        See documentation for IRC.remove_global_handler.
        """
        self.irclibobj.remove_global_handler(*args)

    def action(self, target, action):
        """Send a CTCP ACTION command."""
        self.ctcp("ACTION", target, action)

    def admin(self, server=""):
        """Send an ADMIN command."""
        self.send_raw(" ".join(["ADMIN", server]).strip())

    def ctcp(self, ctcptype, target, parameter=""):
        """Send a CTCP command."""
        ctcptype = ctcptype.upper()
        self.privmsg(target, "\001%s%s\001" % (ctcptype, parameter and (" " + parameter) or ""))

    def ctcp_reply(self, target, parameter):
        """Send a CTCP REPLY command."""
        self.notice(target, "\001%s\001" % parameter)

    def disconnect(self, message="", volontary=False):
        """Hang up the connection.

        Arguments:

            message -- Quit message.
        """

        self.lock.acquire()

        if self.connected:
            self.connected = False
        if self.logged_in:
            self.logged_in = False

        if self.socket and self.socket != 'closed':
            if message and message != 'Connection reset by peer':
                self.quit(message)

            try:
                self.socket.close()
            except socket.error, x:
                pass
            self.socket = 'closed'

        self.lock.release()

        if volontary == False:
            self._handle_event(Event("disconnect", self.server, "", [message]))

    def globops(self, text):
        """Send a GLOBOPS command."""
        self.send_raw("GLOBOPS :" + text)

    def info(self, server=""):
        """Send an INFO command."""
        self.send_raw(" ".join(["INFO", server]).strip())

    def invite(self, nick, channel):
        """Send an INVITE command."""
        self.send_raw(" ".join(["INVITE", nick, channel]).strip())

    def ison(self, nicks):
        """Send an ISON command.

        Arguments:

            nicks -- List of nicks.
        """
        self.send_raw("ISON " + " ".join(nicks))

    def join(self, channel_name, callback=None, key=""):
        """Send a JOIN command."""
        if not self.channels.has_key(channel_name):
            self.channels[channel_name] = Channel(self, channel_name)
        self.channels[channel_name].join(key=key, callback=callback)

    def kick(self, channel, nick, comment=""):
        """Send a KICK command."""
        self.send_raw("KICK %s %s%s" % (channel, nick, (comment and (" :" + comment))))

    def links(self, remote_server="", server_mask=""):
        """Send a LINKS command."""
        command = "LINKS"
        if remote_server:
            command = command + " " + remote_server
        if server_mask:
            command = command + " " + server_mask
        self.send_raw(command)

    def list(self, channels=None, server=""):
        """Send a LIST command."""
        command = "LIST"
        if channels:
            command = command + " " + ",".join(channels)
        if server:
            command = command + " " + server
        self.send_raw(command)

    def lusers(self, server=""):
        """Send a LUSERS command."""
        self.send_raw("LUSERS" + (server and (" " + server)))

    def mode(self, target, command):
        """Send a MODE command."""
        self.send_raw("MODE %s %s" % (target, command))

    def motd(self, server=""):
        """Send an MOTD command."""
        self.send_raw("MOTD" + (server and (" " + server)))

    def names(self, channels=None):
        """Send a NAMES command."""
        self.send_raw("NAMES" + (channels and (" " + ",".join(channels)) or ""))

    def nick(self, newnick, callback=None):
        """Send a NICK command."""
        if callback != None:
            self.add_nick_callback(callback)
        if re.search('[ \.\']', newnick) != None:
            self._call_nick_callbacks('erroneusnickname')
            return False
        try:
            str(newnick)
        except:
            self._call_nick_callbacks('erroneusnickname')
            return False
        self.new_nickname = newnick
        self.send_raw("NICK " + newnick)
        return True

    def notice(self, target, text):
        """Send a NOTICE command."""
        # Should limit len(text) here!
        self.send_raw("NOTICE %s :%s" % (target, text))

    def oper(self, nick, password):
        """Send an OPER command."""
        self.send_raw("OPER %s %s" % (nick, password))

    def part(self, channels, message=""):
        """Send a PART command."""
        try:
            if isinstance(channels, basestring):
                try:
                    self.channels[channels].part(message=message)
                except KeyError:
                    raise UnknownChannel, (channels, message, str(self))
            else:
                for channel in channels:
                    try:
                        self.channels[channel].part(message=message)
                    except KeyError:
                        raise UnknownChannel, (channel, message, self)
        except ServerNotConnectedError:
            self.disconnect(volontary=True)
            self.connect()

    def pass_(self, password):
        """Send a PASS command."""
        self.send_raw("PASS " + password)

    def ping(self, target, target2=""):
        """Send a PING command."""
        self.send_raw("PING %s%s" % (target, target2 and (" " + target2)))

    def pong(self, target, target2=""):
        """Send a PONG command."""
        self.send_raw("PONG %s%s" % (target, target2 and (" " + target2)))

    def privmsg(self, target, text):
        """Send a PRIVMSG command."""
        for l in text.split('\n'):
            l_size = len(l.encode('utf-8'))
            available_size = float(510-len('%s PRIVMSG %s :' % (self.irc_id, target)))  # 510 is the size limit for IRC messages defined in RFC 2812
            e = 0
            for i in range(int(math.ceil(l_size/available_size))):
                s = e
                e = s+int(available_size)
                while len(l[s:e].encode('utf-8')) >= available_size:
                    e -= 1
                self.send_raw("PRIVMSG %s :%s" % (target, l[s:e]))

    def privmsg_many(self, targets, text):
        """Send a PRIVMSG command to multiple targets."""
        # Size of targets should be limited
        self.privmsg(','.join(targets), text)

    def quit(self, message=""):
        """Send a QUIT command."""
        # Note that many IRC servers don't use your QUIT message
        # unless you've been connected for at least 5 minutes!
        try:
            self.send_raw("QUIT" + (message and (" :" + message)))
        except ServerNotConnectedError:
            pass

    def send_raw(self, string):
        """Send raw string to the server.

        The string will be padded with appropriate CR LF.
        """
        if not self.socket or isinstance(self.socket, basestring):
            raise ServerNotConnectedError, self
        try:
            if self.ssl:
                self.ssl.write(string.encode('utf-8') + "\r\n")
            else:
                self.socket.send(string.encode('utf-8') + "\r\n")
            if DEBUG:
                print "TO SERVER:", string
        except socket.error, x:
            # Ouch!
            self.disconnect("Connection reset by peer")

    def squit(self, server, comment=""):
        """Send an SQUIT command."""
        self.send_raw("SQUIT %s%s" % (server, comment and (" :" + comment)))

    def stats(self, statstype, server=""):
        """Send a STATS command."""
        self.send_raw("STATS %s%s" % (statstype, server and (" " + server)))

    def time(self, server=""):
        """Send a TIME command."""
        self.send_raw("TIME" + (server and (" " + server)))

    def topic(self, channel, new_topic=None):
        """Send a TOPIC command."""
        if new_topic is None:
            self.send_raw("TOPIC " + channel)
        else:
            self.send_raw("TOPIC %s :%s" % (channel, new_topic))

    def trace(self, target=""):
        """Send a TRACE command."""
        self.send_raw("TRACE" + (target and (" " + target)))

    def user(self, username, realname):
        """Send a USER command."""
        self.send_raw("USER %s 0 * :%s" % (username, realname))

    def userhost(self, nicks):
        """Send a USERHOST command."""
        self.send_raw("USERHOST " + ",".join(nicks))

    def users(self, server=""):
        """Send a USERS command."""
        self.send_raw("USERS" + (server and (" " + server)))

    def version(self, server=""):
        """Send a VERSION command."""
        self.send_raw("VERSION" + (server and (" " + server)))

    def wallops(self, text):
        """Send a WALLOPS command."""
        self.send_raw("WALLOPS :" + text)

    def who(self, target="", op=""):
        """Send a WHO command."""
        self.send_raw("WHO%s%s" % (target and (" " + target), op and (" o")))

    def whois(self, targets):
        """Send a WHOIS command."""
        self.send_raw("WHOIS " + ",".join(targets))

    def whowas(self, nick, max="", server=""):
        """Send a WHOWAS command."""
        self.send_raw("WHOWAS %s%s%s" % (nick,
                                         max and (" " + max),
                                         server and (" " + server)))

class DCCConnectionError(IRCError):
    pass


class DCCConnection(Connection):
    """This class represents a DCC connection.

    DCCConnection objects are instantiated by calling the dcc
    method on an IRC object.
    """
    def __init__(self, irclibobj, dcctype):
        Connection.__init__(self, irclibobj)
        self.connected = False
        self.passive = 0
        self.dcctype = dcctype
        self.peeraddress = None
        self.peerport = None

    def connect(self, address, port):
        """Connect/reconnect to a DCC peer.

        Arguments:
            address -- Host/IP address of the peer.

            port -- The port number to connect to.

        Returns the DCCConnection object.
        """
        self.peeraddress = socket.gethostbyname(address)
        self.peerport = port
        self.socket = None
        self.previous_buffer = ""
        self.handlers = {}
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.passive = 0
        try:
            self.socket.connect((self.peeraddress, self.peerport))
        except socket.error, x:
            raise DCCConnectionError, "Couldn't connect to socket: %s" % x
        self.connected = True
        if self.irclibobj.fn_to_add_socket:
            self.irclibobj.fn_to_add_socket(self.socket)
        return self

    def listen(self):
        """Wait for a connection/reconnection from a DCC peer.

        Returns the DCCConnection object.

        The local IP address and port are available as
        self.localaddress and self.localport.  After connection from a
        peer, the peer address and port are available as
        self.peeraddress and self.peerport.
        """
        self.previous_buffer = ""
        self.handlers = {}
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.passive = 1
        try:
            self.socket.bind((socket.gethostbyname(socket.gethostname()), 0))
            self.localaddress, self.localport = self.socket.getsockname()
            self.socket.listen(10)
        except socket.error, x:
            raise DCCConnectionError, "Couldn't bind socket: %s" % x
        return self

    def disconnect(self, message=""):
        """Hang up the connection and close the object.

        Arguments:

            message -- Quit message.
        """
        if not self.connected:
            return

        self.connected = False
        try:
            self.socket.close()
        except socket.error, x:
            pass
        self.socket = 'closed'
        self.irclibobj._handle_event(
            self,
            Event("dcc_disconnect", self.peeraddress, "", [message]))
        self.irclibobj._remove_connection(self)

    def process_data(self):
        """[Internal]"""

        if self.passive and not self.connected:
            conn, (self.peeraddress, self.peerport) = self.socket.accept()
            self.socket.close()
            self.socket = conn
            self.connected = True
            if DEBUG:
                print "DCC connection from %s:%d" % (
                    self.peeraddress, self.peerport)
            self.irclibobj._handle_event(
                self,
                Event("dcc_connect", self.peeraddress, None, None))
            return

        try:
            new_data = self.socket.recv(2**14)
        except socket.error, x:
            # The server hung up.
            self.disconnect("Connection reset by peer")
            return
        if not new_data:
            # Read nothing: connection must be down.
            self.disconnect("Connection reset by peer")
            return

        if self.dcctype == "chat":
            # The specification says lines are terminated with LF, but
            # it seems safer to handle CR LF terminations too.
            chunks = _linesep_regexp.split(self.previous_buffer + new_data)

            # Save the last, unfinished line.
            self.previous_buffer = chunks[-1]
            if len(self.previous_buffer) > 2**14:
                # Bad peer! Naughty peer!
                self.disconnect()
                return
            chunks = chunks[:-1]
        else:
            chunks = [new_data]

        command = "dccmsg"
        prefix = self.peeraddress
        target = None
        for chunk in chunks:
            if DEBUG:
                print "FROM PEER:", chunk
            arguments = [chunk]
            if DEBUG:
                print "command: %s, source: %s, target: %s, arguments: %s" % (
                    command, prefix, target, arguments)
            self.irclibobj._handle_event(
                self,
                Event(command, prefix, target, arguments))

    def _get_socket(self):
        """[Internal]"""
        return self.socket

    def privmsg(self, string):
        """Send data to DCC peer.

        The string will be padded with appropriate LF if it's a DCC
        CHAT session.
        """
        try:
            self.socket.send(string)
            if self.dcctype == "chat":
                self.socket.send("\n")
            if DEBUG:
                print "TO PEER: %s\n" % string
        except socket.error, x:
            # Ouch!
            self.disconnect("Connection reset by peer")


class Event:
    """Class representing an IRC event."""
    def __init__(self, eventtype, source, target, arguments=None):
        """Constructor of Event objects.

        Arguments:

            eventtype -- A string describing the event.

            source -- The originator of the event (a nick mask or a server).

            target -- The target of the event (a nick or a channel).

            arguments -- Any event specific arguments.
        """
        self._eventtype = eventtype
        self._source = source
        self._target = target
        if arguments:
            self._arguments = arguments
        else:
            self._arguments = []

    def eventtype(self):
        """Get the event type."""
        return self._eventtype

    def source(self):
        """Get the event source."""
        return self._source

    def target(self):
        """Get the event target."""
        return self._target

    def arguments(self):
        """Get the event arguments."""
        return self._arguments

_LOW_LEVEL_QUOTE = "\020"
_CTCP_LEVEL_QUOTE = "\134"
_CTCP_DELIMITER = "\001"

_low_level_mapping = {
    "0": "\000",
    "n": "\n",
    "r": "\r",
    _LOW_LEVEL_QUOTE: _LOW_LEVEL_QUOTE
}

_low_level_regexp = re.compile(_LOW_LEVEL_QUOTE + "(.)")

def mask_matches(nick, mask):
    """Check if a nick matches a mask.

    Returns true if the nick matches, otherwise false.
    """
    nick = irc_lower(nick)
    mask = irc_lower(mask)
    mask = mask.replace("\\", "\\\\")
    for ch in ".$|[](){}+":
        mask = mask.replace(ch, "\\" + ch)
    mask = mask.replace("?", ".")
    mask = mask.replace("*", ".*")
    r = re.compile(mask, re.IGNORECASE)
    return r.match(nick)

_special = "-[]\\`^{}"
nick_characters = string.ascii_letters + string.digits + _special
_ircstring_translation = string.maketrans(string.ascii_uppercase + "[]\\^",
                                          string.ascii_lowercase + "{}|~")

def irc_lower(s):
    """Returns a lowercased string.

    The definition of lowercased comes from the IRC specification (RFC
    1459).
    """
    return s.translate(_ircstring_translation)

def _ctcp_dequote(message):
    """[Internal] Dequote a message according to CTCP specifications.

    The function returns a list where each element can be either a
    string (normal message) or a tuple of one or two strings (tagged
    messages).  If a tuple has only one element (ie is a singleton),
    that element is the tag; otherwise the tuple has two elements: the
    tag and the data.

    Arguments:

        message -- The message to be decoded.
    """

    def _low_level_replace(match_obj):
        ch = match_obj.group(1)

        # If low_level_mapping doesn't have the character as key, we
        # should just return the character.
        return _low_level_mapping.get(ch, ch)

    if _LOW_LEVEL_QUOTE in message:
        # Yup, there was a quote.  Release the dequoter, man!
        message = _low_level_regexp.sub(_low_level_replace, message)

    if _CTCP_DELIMITER not in message:
        return [message]
    else:
        # Split it into parts.  (Does any IRC client actually *use*
        # CTCP stacking like this?)
        chunks = message.split(_CTCP_DELIMITER)

        messages = []
        i = 0
        while i < len(chunks)-1:
            # Add message if it's non-empty.
            if len(chunks[i]) > 0:
                messages.append(chunks[i])

            if i < len(chunks)-2:
                # Aye!  CTCP tagged data ahead!
                messages.append(tuple(chunks[i+1].split(" ", 1)))

            i = i + 2

        if len(chunks) % 2 == 0:
            # Hey, a lonely _CTCP_DELIMITER at the end!  This means
            # that the last chunk, including the delimiter, is a
            # normal message!  (This is according to the CTCP
            # specification.)
            messages.append(_CTCP_DELIMITER + chunks[-1])

        return messages

def is_channel(string):
    """Check if a string is a channel name.

    Returns true if the argument is a channel name, otherwise false.
    """
    return string and string[0] in "#&+!"

def ip_numstr_to_quad(num):
    """Convert an IP number as an integer given in ASCII
    representation (e.g. '3232235521') to an IP address string
    (e.g. '192.168.0.1')."""
    n = long(num)
    p = map(str, map(int, [n >> 24 & 0xFF, n >> 16 & 0xFF,
                           n >> 8 & 0xFF, n & 0xFF]))
    return ".".join(p)

def ip_quad_to_numstr(quad):
    """Convert an IP address string (e.g. '192.168.0.1') to an IP
    number as an integer given in ASCII representation
    (e.g. '3232235521')."""
    p = map(long, quad.split("."))
    s = str((p[0] << 24) | (p[1] << 16) | (p[2] << 8) | p[3])
    if s[-1] == "L":
        s = s[:-1]
    return s

def nm_to_n(s):
    """Get the nick part of a nickmask.

    (The source of an Event is a nickmask.)
    """
    return s.split("!")[0]

def nm_to_uh(s):
    """Get the userhost part of a nickmask.

    (The source of an Event is a nickmask.)
    """
    return s.split("!")[1]

def nm_to_h(s):
    """Get the host part of a nickmask.

    (The source of an Event is a nickmask.)
    """
    return s.split("@")[1]

def nm_to_u(s):
    """Get the user part of a nickmask.

    (The source of an Event is a nickmask.)
    """
    s = s.split("!")[1]
    return s.split("@")[0]

def parse_nick_modes(mode_string):
    """Parse a nick mode string.

    The function returns a list of lists with three members: sign,
    mode and argument.  The sign is \"+\" or \"-\".  The argument is
    always None.

    Example:

    >>> irclib.parse_nick_modes(\"+ab-c\")
    [['+', 'a', None], ['+', 'b', None], ['-', 'c', None]]
    """

    return _parse_modes(mode_string, "")

def parse_channel_modes(mode_string):
    """Parse a channel mode string.

    The function returns a list of lists with three members: sign,
    mode and argument.  The sign is \"+\" or \"-\".  The argument is
    None if mode isn't one of \"b\", \"k\", \"l\", \"v\" or \"o\".

    Example:

    >>> irclib.parse_channel_modes(\"+ab-c foo\")
    [['+', 'a', None], ['+', 'b', 'foo'], ['-', 'c', None]]
    """

    return _parse_modes(mode_string, "bklvo")

def _parse_modes(mode_string, unary_modes=""):
    """[Internal]"""
    modes = []
    arg_count = 0

    # State variable.
    sign = ""

    a = mode_string.split()
    if len(a) == 0:
        return []
    else:
        mode_part, args = a[0], a[1:]

    if mode_part[0] not in "+-":
        return []
    for ch in mode_part:
        if ch in "+-":
            sign = ch
        elif ch == " ":
            collecting_arguments = 1
        elif ch in unary_modes:
            if len(args) >= arg_count + 1:
                modes.append([sign, ch, args[arg_count]])
                arg_count = arg_count + 1
            else:
                modes.append([sign, ch, None])
        else:
            modes.append([sign, ch, None])
    return modes

def _ping_ponger(connection, event):
    """[Internal]"""
    connection.pong(event.target())

# Numeric table mostly stolen from the Perl IRC module (Net::IRC).
numeric_events = {
    "001": "welcome",
    "002": "yourhost",
    "003": "created",
    "004": "myinfo",
    "005": "featurelist",  # XXX
    "200": "tracelink",
    "201": "traceconnecting",
    "202": "tracehandshake",
    "203": "traceunknown",
    "204": "traceoperator",
    "205": "traceuser",
    "206": "traceserver",
    "207": "traceservice",
    "208": "tracenewtype",
    "209": "traceclass",
    "210": "tracereconnect",
    "211": "statslinkinfo",
    "212": "statscommands",
    "213": "statscline",
    "214": "statsnline",
    "215": "statsiline",
    "216": "statskline",
    "217": "statsqline",
    "218": "statsyline",
    "219": "endofstats",
    "221": "umodeis",
    "231": "serviceinfo",
    "232": "endofservices",
    "233": "service",
    "234": "servlist",
    "235": "servlistend",
    "241": "statslline",
    "242": "statsuptime",
    "243": "statsoline",
    "244": "statshline",
    "250": "luserconns",
    "251": "luserclient",
    "252": "luserop",
    "253": "luserunknown",
    "254": "luserchannels",
    "255": "luserme",
    "256": "adminme",
    "257": "adminloc1",
    "258": "adminloc2",
    "259": "adminemail",
    "261": "tracelog",
    "262": "endoftrace",
    "263": "tryagain",
    "265": "n_local",
    "266": "n_global",
    "300": "none",
    "301": "away",
    "302": "userhost",
    "303": "ison",
    "305": "unaway",
    "306": "nowaway",
    "311": "whoisuser",
    "312": "whoisserver",
    "313": "whoisoperator",
    "314": "whowasuser",
    "315": "endofwho",
    "316": "whoischanop",
    "317": "whoisidle",
    "318": "endofwhois",
    "319": "whoischannels",
    "321": "liststart",
    "322": "list",
    "323": "listend",
    "324": "channelmodeis",
    "329": "channelcreate",
    "331": "notopic",
    "332": "currenttopic",
    "333": "topicinfo",
    "341": "inviting",
    "342": "summoning",
    "346": "invitelist",
    "347": "endofinvitelist",
    "348": "exceptlist",
    "349": "endofexceptlist",
    "351": "version",
    "352": "whoreply",
    "353": "namreply",
    "361": "killdone",
    "362": "closing",
    "363": "closeend",
    "364": "links",
    "365": "endoflinks",
    "366": "endofnames",
    "367": "banlist",
    "368": "endofbanlist",
    "369": "endofwhowas",
    "371": "info",
    "372": "motd",
    "373": "infostart",
    "374": "endofinfo",
    "375": "motdstart",
    "376": "endofmotd",
    "377": "motd2",        # 1997-10-16 -- tkil
    "381": "youreoper",
    "382": "rehashing",
    "384": "myportis",
    "391": "time",
    "392": "usersstart",
    "393": "users",
    "394": "endofusers",
    "395": "nousers",
    "401": "nosuchnick",
    "402": "nosuchserver",
    "403": "nosuchchannel",
    "404": "cannotsendtochan",
    "405": "toomanychannels",
    "406": "wasnosuchnick",
    "407": "toomanytargets",
    "409": "noorigin",
    "411": "norecipient",
    "412": "notexttosend",
    "413": "notoplevel",
    "414": "wildtoplevel",
    "421": "unknowncommand",
    "422": "nomotd",
    "423": "noadmininfo",
    "424": "fileerror",
    "431": "nonicknamegiven",
    "432": "erroneusnickname", # Thiss iz how its speld in thee RFC.
    "433": "nicknameinuse",
    "436": "nickcollision",
    "437": "unavailresource",  # "Nick temporally unavailable"
    "441": "usernotinchannel",
    "442": "notonchannel",
    "443": "useronchannel",
    "444": "nologin",
    "445": "summondisabled",
    "446": "usersdisabled",
    "451": "notregistered",
    "461": "needmoreparams",
    "462": "alreadyregistered",
    "463": "nopermforhost",
    "464": "passwdmismatch",
    "465": "yourebannedcreep", # I love this one...
    "466": "youwillbebanned",
    "467": "keyset",
    "471": "channelisfull",
    "472": "unknownmode",
    "473": "inviteonlychan",
    "474": "bannedfromchan",
    "475": "badchannelkey",
    "476": "badchanmask",
    "477": "nochanmodes",  # "Channel doesn't support modes"
    "478": "banlistfull",
    "481": "noprivileges",
    "482": "chanoprivsneeded",
    "483": "cantkillserver",
    "484": "restricted",   # Connection is restricted
    "485": "uniqopprivsneeded",
    "491": "nooperhost",
    "492": "noservicehost",
    "501": "umodeunknownflag",
    "502": "usersdontmatch",
}

generated_events = [
    # Generated events
    "dcc_connect",
    "dcc_disconnect",
    "dccmsg",
    "disconnect",
    "ctcp",
    "ctcpreply",
]

protocol_events = [
    # IRC protocol events
    "error",
    "join",
    "kick",
    "mode",
    "part",
    "ping",
    "privmsg",
    "privnotice",
    "pubmsg",
    "pubnotice",
    "quit",
    "invite",
    "pong",
]

all_events = generated_events + protocol_events + numeric_events.values()
