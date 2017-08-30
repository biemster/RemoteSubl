import sublime
import sublime_plugin
import os
import tempfile
import socket
import subprocess
from time import strftime
from threading import Thread
try:
    import socketserver
except ImportError:
    import SocketServer as socketserver


FILES = {}
server = None


def subl(*args):
    executable_path = sublime.executable_path()
    if sublime.platform() == 'osx':
        app_path = executable_path[:executable_path.rfind('.app/') + 5]
        executable_path = app_path + 'Contents/SharedSupport/bin/subl'

    subprocess.Popen([executable_path] + list(args))

    def on_activated():
        if sublime.platform() == 'windows':
            # refocus sublime text window
            subprocess.Popen([executable_path, "--command", ""])
        window = sublime.active_window()
        view = window.active_view()
        sublime_plugin.on_activated(view.id())
        sublime_plugin.on_activated_async(view.id())

    sublime.set_timeout(on_activated, 100)


def say(msg):
    print('[remote_subl {}]: {}'.format(strftime("%H:%M:%S"), msg))


class File:
    def __init__(self, session):
        self.session = session
        self.env = {}
        self.data = b""
        self.ready = False

    def append(self, line):
        if len(self.data) < self.file_size:
            self.data += line

        if len(self.data) >= self.file_size:
            self.data = self.data[:self.file_size]
            self.ready = True

    def close(self, remove=True):
        self.session.socket.send(b"close\n")
        self.session.socket.send(b"token: " + self.env['token'].encode("utf8") + b"\n")
        self.session.socket.send(b"\n")
        if remove:
            os.unlink(self.temp_path)
            os.rmdir(self.temp_dir)
        self.session.try_close()

    def save(self):
        self.session.socket.send(b"save\n")
        self.session.socket.send(b"token: " + self.env['token'].encode("utf8") + b"\n")
        temp_file = open(self.temp_path, "rb")
        new_file = temp_file.read()
        temp_file.close()
        self.session.socket.send(b"data: " + str(len(new_file)).encode("utf8") + b"\n")
        self.session.socket.send(new_file)
        self.session.socket.send(b"\n")

    def get_temp_dir(self):
        # First determine if the file has been sent before.
        for f in FILES.values():
            if f.env["real-path"] == self.env["real-path"] and \
                    f.host and f.host == self.host:
                return f.temp_dir

        # Create a secure temporary directory, both for privacy and to allow
        # multiple files with the same basename to be edited at once without
        # overwriting each other.
        try:
            return tempfile.mkdtemp(prefix='remote_subl-')
        except OSError as e:
            sublime.error_message(
                'Failed to create remote_subl temporary directory! Error: {}'.format(e))

    def open(self):
        self.temp_dir = self.get_temp_dir()
        self.temp_path = os.path.join(
            self.temp_dir,
            self.base_name)
        try:
            temp_file = open(self.temp_path, "wb+")
            temp_file.write(self.data)
            temp_file.flush()
            temp_file.close()
        except IOError as e:
            print(e)
            # Remove the file if it exists.
            if os.path.exists(self.temp_path):
                os.remove(self.temp_path)
            try:
                os.rmdir(self.temp_dir)
            except OSError:
                pass

            sublime.error_message('Failed to write to temp file! Error: %s' % str(e))

        # create new window if needed
        if len(sublime.windows()) == 0 or "new" in self.env:
            sublime.run_command("new_window")

        # Open it within sublime
        view = sublime.active_window().open_file(
            "{0}:{1}:0".format(
                self.temp_path, self.env['selection'] if 'selection' in self.env else 0),
            sublime.ENCODED_POSITION)

        # Add the file metadata to the view's settings
        view.settings().set('remote_subl.host', self.host)
        view.settings().set('remote_subl.base_name', self.base_name)

        # if the current view is attahced to another file object,
        # that file object has to be closed first.
        if view.id() in FILES:
            file = FILES.pop(view.id())
            try:
                # connection may have lost
                file.close(remove=False)
            except:
                pass

        # Add the file to the global list
        FILES[view.id()] = self

        # Bring sublime to front by running `subl --command ""`
        subl("--command", "")
        view.run_command("remote_subl_update_status_bar")


class Session:
    def __init__(self, socket):
        self.socket = socket
        self.parsing_data = False
        self.nconn = 0
        self.file = None

    def parse_input(self, input_line):
        if input_line.strip() == b"open":
            self.file = File(self)
            self.nconn += 1
            return

        if self.parsing_data:
            self.file.append(input_line)
            if self.file.ready:
                self.file.open()
                self.parsing_data = False
                self.file = None
            return

        if not self.file:
            return

        # prase settings
        input_line = input_line.decode("utf8").strip()
        if ":" not in input_line:
            # not a setting
            return

        k, v = input_line.split(":", 1)
        k = k.strip()
        v = v.strip()
        self.file.env[k] = v

        if k == "data":
            self.file.file_size = int(v)
            self.parsing_data = True
        elif k == "display-name":
            if ":" in v:
                self.file.host, self.file.base_name = v.split(":")
            else:
                self.file.host = None
                self.file.base_name = v

    def try_close(self):
        self.nconn -= 1
        if self.nconn == 0:
            self.socket.shutdown(socket.SHUT_RDWR)
            self.socket.close()


class RemoteSublEventListener(sublime_plugin.EventListener):
    def on_post_save_async(self, view):
        base_name = view.settings().get('remote_subl.base_name')
        if base_name:
            host = view.settings().get('remote_subl.host', "remote server")
            try:
                file = FILES[view.id()]
                file.save()
                say('Saved {} to {}.'.format(base_name, host))

                sublime.set_timeout(
                    lambda: sublime.status_message("Saved {} to {}.".format(
                        base_name, host)))
            except:
                say('Error saving {} to {}.'.format(base_name, host))
                sublime.set_timeout(
                    lambda: sublime.status_message(
                        "Error saving {} to {}.".format(base_name, host)))

    def on_close(self, view):
        base_name = view.settings().get('remote_subl.base_name')
        if base_name:
            host = view.settings().get('remote_subl.host', "remote server")
            try:
                file = FILES.pop(view.id())
                file.close()
                say('Closed {} in {}.'.format(base_name, host))
            except:
                say('Error closing {} in {}.'.format(base_name, host))

    def on_activated(self, view):
        base_name = view.settings().get('remote_subl.base_name')
        if base_name:
            view.run_command("remote_subl_update_status_bar")


class RemoteSublUpdateStatusBarCommand(sublime_plugin.TextCommand):

    def run(self, edit):
        view = self.view
        if view.id() in FILES:
            file = FILES[view.id()]
            server_name = file.host or "remote server"
            self.view.set_status("remotesub_status", "[{}]".format(server_name))
        else:
            self.view.erase_status("remotesub_status")


class ConnectionHandler(socketserver.BaseRequestHandler):
    def handle(self):
        say('New connection from ' + str(self.client_address))

        session = Session(self.request)
        self.request.send(b"Sublime Text 3 (remote_subl plugin)\n")

        socket_fd = self.request.makefile("rb")
        while True:
            line = socket_fd.readline()
            if(len(line) == 0):
                break
            session.parse_input(line)

        say('Connection from {} is closed.'.format(str(self.client_address)))


class TCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True


def unload_handler():
    global server
    say('Killing server...')
    if server:
        server.shutdown()
        server.server_close()


def plugin_loaded():
    global server

    # Load settings
    settings = sublime.load_settings("remote_subl.sublime-settings")
    port = settings.get("port", 52698)
    host = settings.get("host", "localhost")

    # Start server thread
    server = TCPServer((host, port), ConnectionHandler)
    Thread(target=server.serve_forever, args=[]).start()
    say('Server running on {}:{} ...'.format(host, str(port)))
