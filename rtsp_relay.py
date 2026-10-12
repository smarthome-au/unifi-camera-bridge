"""Bounded TCP relay for RTSP + interleaved RTP. No credential storage.

The camera checks authentication. Requests, Digest headers, SDP and video
bytes are forwarded unchanged; this module never decodes video. Protect's
support log confirms forceTcp=true. RTP-over-UDP is not relayed.
"""
import datetime
import ipaddress
import json
import select
import socket
import socketserver
import threading
import time
import urllib.parse


def select_destination(client, native, lq):
    """Select once, before any RTSP session exists; preserve the request bytes.

    Some NVRs use one RTSP port for both ONVIF profiles. Only the optional
    converter uses this parser. Wildcard OPTIONS receives a local response;
    subsequent DESCRIBE selects the server that will authenticate the client.
    """
    pending = b''
    for _ in range(8):
        while b'\r\n\r\n' not in pending:
            chunk = client.recv(4096)
            if not chunk: raise OSError('Incomplete RTSP request')
            pending += chunk
            if len(pending) > 65536: raise OSError('RTSP headers too large')
        header, tail = pending.split(b'\r\n\r\n', 1)
        lines = header.split(b'\r\n')
        try:
            method, uri, version = lines[0].decode('ascii').split(' ')
            if version != 'RTSP/1.0': raise ValueError()
            path = urllib.parse.urlsplit(uri).path
            headers = dict(line.split(b':', 1) for line in lines[1:])
            headers = {k.strip().lower(): v.strip() for k,v in headers.items()}
            length = int(headers.get(b'content-length', b'0'))
            cseq = headers.get(b'cseq', b'')
            if not cseq.isdigit() or length != 0: raise ValueError()
        except (ValueError, UnicodeError):
            raise OSError('Unsupported initial RTSP request') from None
        if method == 'OPTIONS' and path in {'', '/', '*'}:
            client.sendall(b'RTSP/1.0 200 OK\r\nCSeq: '+cseq+b'\r\nPublic: OPTIONS, DESCRIBE, SETUP, PLAY, PAUSE, TEARDOWN, GET_PARAMETER\r\n\r\n')
            pending = tail
            continue
        target = lq if any(path == p or path.startswith(p + '/') for p in ['/av_stream0/sec', '/av_stream1/sec']) else native
        return target, pending
    raise OSError('Too many initial RTSP requests')


class RTSPRelay(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, camera, camera_port=554, allowed=(), lq_target=None, lq_source_address=None):
        self.camera = camera
        self.camera_port = camera_port
        self.allowed = allowed
        self.lq_target = lq_target
        self.lq_source_address = lq_source_address
        self.slots = threading.BoundedSemaphore(16)
        self.lock = threading.Lock()
        self.active = 0
        self.to_camera = 0
        self.from_camera = 0
        self.sessions = 0
        super().__init__(address, RelayHandler)

    def stats(self):
        with self.lock:
            return {'active_connections': self.active, 'sessions': self.sessions,
                    'bytes_to_camera': self.to_camera, 'bytes_from_camera': self.from_camera}


class RelayHandler(socketserver.BaseRequestHandler):
    def handle(self):
        server = self.server
        if server.allowed and not any(ipaddress.ip_address(self.client_address[0]) in n for n in server.allowed):
            return
        if not server.slots.acquire(blocking=False):
            return
        upstream = None
        sent = received = 0
        outcome = 'closed'
        start = time.monotonic()
        with server.lock:
            server.active += 1
            server.sessions += 1
            session = server.sessions
        print(json.dumps({'time': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                          'rtsp': 'open', 'session': session}), flush=True)
        try:
            self.request.settimeout(15)
            target, initial = (server.camera, server.camera_port), b''
            if server.lq_target:
                target, initial = select_destination(self.request, target, server.lq_target)
            source = (server.lq_source_address, 0) if target == server.lq_target and server.lq_source_address else None
            upstream = socket.create_connection(target, timeout=8, source_address=source)
            upstream.settimeout(15)
            if initial:
                upstream.sendall(initial)
                sent += len(initial)
            while True:
                readable, _, _ = select.select([self.request, upstream], [], [], 120)
                if not readable:
                    outcome = 'idle_timeout'
                    break
                eof = False
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        eof = True
                        break
                    if source is self.request:
                        upstream.sendall(data)
                        sent += len(data)
                        with server.lock:
                            server.to_camera += len(data)
                    else:
                        self.request.sendall(data)
                        received += len(data)
                        with server.lock:
                            server.from_camera += len(data)
                if eof:
                    break
        except OSError:
            outcome = 'connection_error'
        finally:
            if upstream:
                upstream.close()
            with server.lock:
                server.active -= 1
            server.slots.release()
            print(json.dumps({'time': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                              'rtsp': outcome, 'session': session,
                              'seconds': round(time.monotonic() - start, 1),
                              'bytes_to_camera': sent, 'bytes_from_camera': received}), flush=True)
