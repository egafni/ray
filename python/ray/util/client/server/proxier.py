import atexit
from concurrent import futures
from dataclasses import dataclass
import grpc
import logging
import json
import psutil
from queue import Queue
import socket
import sys
from threading import Thread, RLock
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import ray
from ray.cloudpickle.compat import pickle
from ray.job_config import JobConfig
import ray.core.generated.ray_client_pb2 as ray_client_pb2
import ray.core.generated.ray_client_pb2_grpc as ray_client_pb2_grpc
from ray.util.client.common import (ClientServerHandle,
                                    CLIENT_SERVER_MAX_THREADS, GRPC_OPTIONS)
from ray._private.services import ProcessInfo, start_ray_client_server
from ray._private.utils import detect_fate_sharing_support

logger = logging.getLogger(__name__)

CHECK_PROCESS_INTERVAL_S = 30

MIN_SPECIFIC_SERVER_PORT = 23000
MAX_SPECIFIC_SERVER_PORT = 24000

CHECK_CHANNEL_TIMEOUT_S = 10


def _get_client_id_from_context(context: Any) -> str:
    """
    Get `client_id` from gRPC metadata. If the `client_id` is not present,
    this function logs an error and sets the status_code.
    """
    metadata = {k: v for k, v in context.invocation_metadata()}
    client_id = metadata.get("client_id") or ""
    if client_id == "":
        logger.error("Client connecting with no client_id")
        context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
    return client_id


@dataclass
class SpecificServer:
    port: int
    process_handle_future: futures.Future
    channel: "grpc._channel.Channel"

    def wait_ready(self, timeout: Optional[float] = None) -> None:
        """
        Wait for the server to actually start up.
        """
        self.process_handle_future.result(timeout=timeout)
        return

    def process_handle(self) -> ProcessInfo:
        return self.process_handle_future.result()


class ProxyManager():
    def __init__(self, redis_address: str, session_dir: Optional[str] = None):
        self.servers: Dict[str, SpecificServer] = dict()
        self.server_lock = RLock()
        self.redis_address = redis_address
        self._free_ports: List[int] = list(
            range(MIN_SPECIFIC_SERVER_PORT, MAX_SPECIFIC_SERVER_PORT))

        self._check_thread = Thread(target=self._check_processes, daemon=True)
        self._check_thread.start()

        self.fate_share = bool(detect_fate_sharing_support())
        self._session_dir: str = session_dir or ""
        atexit.register(self._cleanup)

    def _get_unused_port(self) -> int:
        """
        Search for a port in _free_ports that is unused.
        """
        with self.server_lock:
            num_ports = len(self._free_ports)
            for _ in range(num_ports):
                port = self._free_ports.pop(0)
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    s.bind(("", port))
                except OSError:
                    self._free_ports.append(port)
                    continue
                finally:
                    s.close()
                return port
        raise RuntimeError("Unable to succeed in selecting a random port.")

    def _get_session_dir(self) -> str:
        """
        Gets the session_dir of this running Ray session. This usually
        looks like /tmp/ray/session_<timestamp>.
        """
        if self._session_dir:
            return self._session_dir
        connection_tuple = ray.init(address=self.redis_address)
        ray.shutdown()
        self._session_dir = connection_tuple["session_dir"]
        return self._session_dir

    def start_specific_server(self, client_id: str,
                              job_config: JobConfig) -> bool:
        """
        Start up a RayClient Server for an incoming client to
        communicate with. Returns whether creation was successful.
        """
        with self.server_lock:
            port = self._get_unused_port()
            handle_ready = futures.Future()
            specific_server = SpecificServer(
                port=port,
                process_handle_future=handle_ready,
                channel=grpc.insecure_channel(
                    f"localhost:{port}", options=GRPC_OPTIONS))
            self.servers[client_id] = specific_server

        serialized_runtime_env = job_config.get_serialized_runtime_env()

        proc = start_ray_client_server(
            self.redis_address,
            port,
            fate_share=self.fate_share,
            server_type="specific-server",
            serialized_runtime_env=serialized_runtime_env,
            session_dir=self._get_session_dir())

        # Wait for the process being run transitions from the shim process
        # to the actual RayClient Server.
        pid = proc.process.pid
        if sys.platform != "win32":
            psutil_proc = psutil.Process(pid)
        else:
            psutil_proc = None
        # Don't use `psutil` on Win32
        while psutil_proc is not None:
            if proc.process.poll() is not None:
                logger.error(
                    f"SpecificServer startup failed for client: {client_id}")
                break
            cmd = psutil_proc.cmdline()
            if len(cmd) > 3 and cmd[2] == "ray.util.client.server":
                break
            logger.debug(
                "Waiting for Process to reach the actual client server.")
            time.sleep(0.5)
        handle_ready.set_result(proc)
        logger.info(f"SpecificServer started on port: {port} with PID: {pid} "
                    f"for client: {client_id}")
        return proc.process.poll() is None

    def _get_server_for_client(self,
                               client_id: str) -> Optional[SpecificServer]:
        with self.server_lock:
            client = self.servers.get(client_id)
            if client is None:
                logger.error(f"Unable to find channel for client: {client_id}")
            return client

    def get_channel(
            self,
            client_id: str,
    ) -> Optional["grpc._channel.Channel"]:
        """
        Find the gRPC Channel for the given client_id. This will block until
        the server process has started.
        """
        server = self._get_server_for_client(client_id)
        if server is None:
            return None
        server.wait_ready()
        try:
            grpc.channel_ready_future(
                server.channel).result(timeout=CHECK_CHANNEL_TIMEOUT_S)
            return server.channel
        except grpc.FutureTimeoutError:
            logger.exception(f"Timeout waiting for channel for {client_id}")
            return None

    def _check_processes(self):
        """
        Keeps the internal servers dictionary up-to-date with running servers.
        """
        while True:
            with self.server_lock:
                for client_id, specific_server in list(self.servers.items()):
                    poll_result = specific_server.process_handle(
                    ).process.poll()
                    if poll_result is not None:
                        del self.servers[client_id]
                        # Port is available to use again.
                        self._free_ports.append(specific_server.port)

            time.sleep(CHECK_PROCESS_INTERVAL_S)

    def _cleanup(self) -> None:
        """
        Forcibly kill all spawned RayClient Servers. This ensures cleanup
        for platforms where fate sharing is not supported.
        """
        for server in self.servers.values():
            try:
                server.wait_ready(0.1)
                server.process_handle().process.kill()
            except TimeoutError:
                # Server has not been started yet.
                pass


class RayletServicerProxy(ray_client_pb2_grpc.RayletDriverServicer):
    def __init__(self, ray_connect_handler: Callable,
                 proxy_manager: ProxyManager):
        self.proxy_manager = proxy_manager
        self.ray_connect_handler = ray_connect_handler

    def _call_inner_function(
            self, request, context,
            method: str) -> Optional[ray_client_pb2_grpc.RayletDriverStub]:
        client_id = _get_client_id_from_context(context)
        chan = self.proxy_manager.get_channel(client_id)
        if not chan:
            logger.error(f"Channel for Client: {client_id} not found!")
            context.set_code(grpc.StatusCode.NOT_FOUND)
            return None

        stub = ray_client_pb2_grpc.RayletDriverStub(chan)
        return getattr(stub, method)(
            request, metadata=[("client_id", client_id)])

    def Init(self, request, context=None) -> ray_client_pb2.InitResponse:
        return self._call_inner_function(request, context, "Init")

    def PrepRuntimeEnv(self, request,
                       context=None) -> ray_client_pb2.PrepRuntimeEnvResponse:
        return self._call_inner_function(request, context, "PrepRuntimeEnv")

    def KVPut(self, request, context=None) -> ray_client_pb2.KVPutResponse:
        return self._call_inner_function(request, context, "KVPut")

    def KVGet(self, request, context=None) -> ray_client_pb2.KVGetResponse:
        return self._call_inner_function(request, context, "KVGet")

    def KVDel(self, request, context=None) -> ray_client_pb2.KVDelResponse:
        return self._call_inner_function(request, context, "KVGet")

    def KVList(self, request, context=None) -> ray_client_pb2.KVListResponse:
        return self._call_inner_function(request, context, "KVList")

    def KVExists(self, request,
                 context=None) -> ray_client_pb2.KVExistsResponse:
        return self._call_inner_function(request, context, "KVExists")

    def ClusterInfo(self, request,
                    context=None) -> ray_client_pb2.ClusterInfoResponse:

        # NOTE: We need to respond to the PING request here to allow the client
        # to continue with connecting.
        if request.type == ray_client_pb2.ClusterInfoType.PING:
            resp = ray_client_pb2.ClusterInfoResponse(json=json.dumps({}))
            return resp
        return self._call_inner_function(request, context, "ClusterInfo")

    def Terminate(self, req, context=None):
        return self._call_inner_function(req, context, "Terminate")

    def GetObject(self, request, context=None):
        return self._call_inner_function(request, context, "GetObject")

    def PutObject(self, request: ray_client_pb2.PutRequest,
                  context=None) -> ray_client_pb2.PutResponse:
        return self._call_inner_function(request, context, "PutObject")

    def WaitObject(self, request, context=None) -> ray_client_pb2.WaitResponse:
        return self._call_inner_function(request, context, "WaitObject")

    def Schedule(self, task, context=None) -> ray_client_pb2.ClientTaskTicket:
        return self._call_inner_function(task, context, "Schedule")


def forward_streaming_requests(grpc_input_generator: Iterator[Any],
                               output_queue: "Queue") -> None:
    """
    Forwards streaming requests from the grpc_input_generator into the
    output_queue.
    """
    try:
        for req in grpc_input_generator:
            output_queue.put(req)
    except grpc.RpcError as e:
        logger.debug("closing dataservicer reader thread "
                     f"grpc error reading request_iterator: {e}")
    finally:
        # Set the sentinel value for the output_queue
        output_queue.put(None)


def ray_client_server_env_prep(job_config: JobConfig) -> JobConfig:
    return job_config


def prepare_runtime_init_req(iterator: Iterator[ray_client_pb2.DataRequest]
                             ) -> Tuple[ray_client_pb2.DataRequest, JobConfig]:
    """
    Extract JobConfig and possibly mutate InitRequest before it is passed to
    the specific RayClient Server.
    """
    init_req = next(iterator)
    init_type = init_req.WhichOneof("type")
    assert init_type == "init", ("Received initial message of type "
                                 f"{init_type}, not 'init'.")
    req = init_req.init
    job_config = JobConfig()
    if req.job_config:
        job_config = pickle.loads(req.job_config)
    new_job_config = ray_client_server_env_prep(job_config)
    modified_init_req = ray_client_pb2.InitRequest(
        job_config=pickle.dumps(new_job_config))

    init_req.init.CopyFrom(modified_init_req)
    return (init_req, new_job_config)


class DataServicerProxy(ray_client_pb2_grpc.RayletDataStreamerServicer):
    def __init__(self, proxy_manager: ProxyManager):
        self.proxy_manager = proxy_manager

    def Datapath(self, request_iterator, context):
        client_id = _get_client_id_from_context(context)
        if client_id == "":
            return

        logger.info(f"New data connection from client {client_id}: ")
        modified_init_req, job_config = prepare_runtime_init_req(
            request_iterator)

        queue = Queue()
        queue.put(modified_init_req)
        if not self.proxy_manager.start_specific_server(client_id, job_config):
            logger.error(f"Server startup failed for client: {client_id}, "
                         f"using JobConfig: {job_config}!")
            context.set_code(grpc.StatusCode.ABORTED)
            return None

        channel = self.proxy_manager.get_channel(client_id)
        if channel is None:
            logger.error(f"Channel not found for {client_id}")
            context.set_code(grpc.StatusCode.NOT_FOUND)
            return None
        stub = ray_client_pb2_grpc.RayletDataStreamerStub(channel)
        thread = Thread(
            target=forward_streaming_requests,
            args=(request_iterator, queue),
            daemon=True)
        thread.start()
        try:
            resp_stream = stub.Datapath(
                iter(queue.get, None), metadata=[("client_id", client_id)])
            for resp in resp_stream:
                yield resp
        finally:
            thread.join(1)


class LogstreamServicerProxy(ray_client_pb2_grpc.RayletLogStreamerServicer):
    def __init__(self, proxy_manager: ProxyManager):
        super().__init__()
        self.proxy_manager = proxy_manager

    def Logstream(self, request_iterator, context):
        client_id = _get_client_id_from_context(context)
        if client_id == "":
            return
        logger.debug(f"New data connection from client {client_id}: ")

        channel = None
        # We need to retry a few times because the LogClient *may* connect
        # Before the DataClient has finished connecting.
        for i in range(5):
            channel = self.proxy_manager.get_channel(client_id)

            if channel is not None:
                break
            logger.warning(
                f"Retrying Logstream connection. {i+1} attempts failed.")
            time.sleep(2)

        if channel is None:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            return None

        stub = ray_client_pb2_grpc.RayletLogStreamerStub(channel)
        queue = Queue()
        thread = Thread(
            target=forward_streaming_requests,
            args=(request_iterator, queue),
            daemon=True)
        thread.start()
        try:
            resp_stream = stub.Logstream(
                iter(queue.get, None), metadata=[("client_id", client_id)])
            for resp in resp_stream:
                yield resp
        finally:
            thread.join(1)


def serve_proxier(connection_str: str, redis_address: str):
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=CLIENT_SERVER_MAX_THREADS),
        options=GRPC_OPTIONS)
    proxy_manager = ProxyManager(redis_address)
    task_servicer = RayletServicerProxy(None, proxy_manager)
    data_servicer = DataServicerProxy(proxy_manager)
    logs_servicer = LogstreamServicerProxy(proxy_manager)
    ray_client_pb2_grpc.add_RayletDriverServicer_to_server(
        task_servicer, server)
    ray_client_pb2_grpc.add_RayletDataStreamerServicer_to_server(
        data_servicer, server)
    ray_client_pb2_grpc.add_RayletLogStreamerServicer_to_server(
        logs_servicer, server)
    server.add_insecure_port(connection_str)
    server.start()
    return ClientServerHandle(
        task_servicer=task_servicer,
        data_servicer=data_servicer,
        logs_servicer=logs_servicer,
        grpc_server=server,
    )
