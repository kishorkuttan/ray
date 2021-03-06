from functools import wraps

import ray
from ray.serve.constants import (DEFAULT_HTTP_HOST, DEFAULT_HTTP_PORT,
                                 SERVE_CONTROLLER_NAME, HTTP_PROXY_TIMEOUT)
from ray.serve.controller import ServeController
from ray.serve.handle import RayServeHandle
from ray.serve.utils import (block_until_http_ready, format_actor_name)
from ray.serve.exceptions import RayServeException
from ray.serve.config import BackendConfig, ReplicaConfig, BackendMetadata
from ray.actor import ActorHandle
from typing import Any, Callable, Dict, List, Optional, Type, Union

controller = None


def _get_controller() -> ActorHandle:
    """Used for internal purpose because using just import serve.global_state
    will always reference the original None object.
    """
    global controller
    if controller is None:
        raise RayServeException("Please run serve.init to initialize or "
                                "connect to existing ray serve cluster.")
    return controller


def _ensure_connected(f: Callable) -> Callable:
    @wraps(f)
    def check(*args, **kwargs):
        _get_controller()
        return f(*args, **kwargs)

    return check


def accept_batch(f: Callable) -> Callable:
    """Annotation to mark a serving function that batch is accepted.

    This annotation need to be used to mark a function expect all arguments
    to be passed into a list.

    Example:

    >>> @serve.accept_batch
        def serving_func(flask_request):
            assert isinstance(flask_request, list)
            ...

    >>> class ServingActor:
            @serve.accept_batch
            def __call__(self, *, python_arg=None):
                assert isinstance(python_arg, list)
    """
    f._serve_accept_batch = True
    return f


def init(name: Optional[str] = None,
         http_host: str = DEFAULT_HTTP_HOST,
         http_port: int = DEFAULT_HTTP_PORT,
         http_middlewares: List[Any] = []) -> None:
    """Initialize or connect to a serve cluster.

    If serve cluster is already initialized, this function will just return.

    If `ray.init` has not been called in this process, it will be called with
    no arguments. To specify kwargs to `ray.init`, it should be called
    separately before calling `serve.init`.

    Args:
        name (str): A unique name for this serve instance. This allows
            multiple serve instances to run on the same ray cluster. Must be
            specified in all subsequent serve.init() calls.
        http_host (str): Host for HTTP servers. Default to "0.0.0.0". Serve
            starts one HTTP server per node in the Ray cluster.
        http_port (int, List[int]): Port for HTTP server. Default to 8000.
    """
    if name is not None and not isinstance(name, str):
        raise TypeError("name must be a string.")

    # Initialize ray if needed.
    if not ray.is_initialized():
        ray.init()

    # Try to get serve controller if it exists
    global controller
    controller_name = format_actor_name(SERVE_CONTROLLER_NAME, name)
    try:
        controller = ray.get_actor(controller_name)
        return
    except ValueError:
        pass

    controller = ServeController.options(
        name=controller_name,
        lifetime="detached",
        max_restarts=-1,
        max_task_retries=-1,
    ).remote(name, http_host, http_port, http_middlewares)

    futures = []
    for node_id in ray.state.node_ids():
        future = block_until_http_ready.options(
            num_cpus=0, resources={
                node_id: 0.01
            }).remote(
                "http://{}:{}/-/routes".format(http_host, http_port),
                timeout=HTTP_PROXY_TIMEOUT)
        futures.append(future)
    ray.get(futures)


@_ensure_connected
def shutdown() -> None:
    """Completely shut down the connected Serve instance.

    Shuts down all processes and deletes all state associated with the Serve
    instance that's currently connected to (via serve.init).
    """
    global controller
    ray.get(controller.shutdown.remote())
    ray.kill(controller, no_restart=True)
    controller = None


@_ensure_connected
def create_endpoint(endpoint_name: str,
                    *,
                    backend: str = None,
                    route: Optional[str] = None,
                    methods: List[str] = ["GET"]) -> None:
    """Create a service endpoint given route_expression.

    Args:
        endpoint_name (str): A name to associate to with the endpoint.
        backend (str, required): The backend that will serve requests to
            this endpoint. To change this or split traffic among backends, use
            `serve.set_traffic`.
        route (str, optional): A string begin with "/". HTTP server will use
            the string to match the path.
        methods(List[str], optional): The HTTP methods that are valid for this
            endpoint.
    """
    if backend is None:
        raise TypeError("backend must be specified when creating "
                        "an endpoint.")
    elif not isinstance(backend, str):
        raise TypeError("backend must be a string, got {}.".format(
            type(backend)))

    if route is not None:
        if not isinstance(route, str) or not route.startswith("/"):
            raise TypeError("route must be a string starting with '/'.")

    if not isinstance(methods, list):
        raise TypeError(
            "methods must be a list of strings, but got type {}".format(
                type(methods)))

    endpoints = list_endpoints()
    if endpoint_name in endpoints:
        methods_old = endpoints[endpoint_name]["methods"]
        route_old = endpoints[endpoint_name]["route"]
        if methods_old.sort() == methods.sort() and route_old == route:
            raise ValueError(
                "Route '{}' is already registered to endpoint '{}' "
                "with methods '{}'.  To set the backend for this "
                "endpoint, please use serve.set_traffic().".format(
                    route, endpoint_name, methods))

    upper_methods = []
    for method in methods:
        if not isinstance(method, str):
            raise TypeError("methods must be a list of strings, but contained "
                            "an element of type {}".format(type(method)))
        upper_methods.append(method.upper())

    ray.get(
        controller.create_endpoint.remote(endpoint_name, {backend: 1.0}, route,
                                          upper_methods))


@_ensure_connected
def delete_endpoint(endpoint: str) -> None:
    """Delete the given endpoint.

    Does not delete any associated backends.
    """
    ray.get(controller.delete_endpoint.remote(endpoint))


@_ensure_connected
def list_endpoints() -> Dict[str, Dict[str, Any]]:
    """Returns a dictionary of all registered endpoints.

    The dictionary keys are endpoint names and values are dictionaries
    of the form: {"methods": List[str], "traffic": Dict[str, float]}.
    """
    return ray.get(controller.get_all_endpoints.remote())


@_ensure_connected
def update_backend_config(
        backend_tag: str,
        config_options: Union[BackendConfig, Dict[str, Any]]) -> None:
    """Update a backend configuration for a backend tag.

    Keys not specified in the passed will be left unchanged.

    Args:
        backend_tag(str): A registered backend.
        config_options(dict, serve.BackendConfig): Backend config options to
            update. Either a BackendConfig object or a dict mapping strings to
            values for the following supported options:
            - "num_replicas": number of worker processes to start up that
            will handle requests to this backend.
            - "max_batch_size": the maximum number of requests that will
            be processed in one batch by this backend.
            - "batch_wait_timeout": time in seconds that backend replicas
            will wait for a full batch of requests before
            processing a partial batch.
            - "max_concurrent_queries": the maximum number of queries
            that will be sent to a replica of this backend
            without receiving a response.
    """

    if not isinstance(config_options, (BackendConfig, dict)):
        raise TypeError(
            "config_options must be a BackendConfig or dictionary.")
    ray.get(
        controller.update_backend_config.remote(backend_tag, config_options))


@_ensure_connected
def get_backend_config(backend_tag: str) -> BackendConfig:
    """Get the backend configuration for a backend tag.

    Args:
        backend_tag(str): A registered backend.
    """
    return ray.get(controller.get_backend_config.remote(backend_tag))


@_ensure_connected
def create_backend(
        backend_tag: str,
        func_or_class: Union[Callable, Type[Callable]],
        *actor_init_args: Any,
        ray_actor_options: Optional[Dict] = None,
        config: Optional[Union[BackendConfig, Dict[str, Any]]] = None) -> None:
    """Create a backend with the provided tag.

    The backend will serve requests with func_or_class.

    Args:
        backend_tag (str): a unique tag assign to identify this backend.
        func_or_class (callable, class): a function or a class implementing
            __call__.
        actor_init_args (optional): the arguments to pass to the class.
            initialization method.
        ray_actor_options (optional): options to be passed into the
            @ray.remote decorator for the backend actor.
        config (dict, serve.BackendConfig, optional): configuration options
            for this backend. Either a BackendConfig, or a dictionary mapping
            strings to values for the following supported options:
            - "num_replicas": number of worker processes to start up that will
            handle requests to this backend.
            - "max_batch_size": the maximum number of requests that will
            be processed in one batch by this backend.
            - "batch_wait_timeout": time in seconds that backend replicas
            will wait for a full batch of requests before processing a
            partial batch.
            - "max_concurrent_queries": the maximum number of queries that will
            be sent to a replica of this backend without receiving a
            response.
    """
    if backend_tag in list_backends():
        raise ValueError(
            "Cannot create backend. "
            "Backend '{}' is already registered.".format(backend_tag))

    if config is None:
        config = {}
    replica_config = ReplicaConfig(
        func_or_class, *actor_init_args, ray_actor_options=ray_actor_options)
    metadata = BackendMetadata(
        accepts_batches=replica_config.accepts_batches,
        is_blocking=replica_config.is_blocking)
    if isinstance(config, dict):
        backend_config = BackendConfig.parse_obj({
            **config, "internal_metadata": metadata
        })
    elif isinstance(config, BackendConfig):
        backend_config = config.copy(update={"internal_metadata": metadata})
    else:
        raise TypeError("config must be a BackendConfig or a dictionary.")
    backend_config._validate_complete()
    ray.get(
        controller.create_backend.remote(backend_tag, backend_config,
                                         replica_config))


@_ensure_connected
def list_backends() -> Dict[str, Dict[str, Any]]:
    """Returns a dictionary of all registered backends.

    Dictionary maps backend tags to backend configs.
    """
    return ray.get(controller.get_all_backends.remote())


@_ensure_connected
def delete_backend(backend_tag: str) -> None:
    """Delete the given backend.

    The backend must not currently be used by any endpoints.
    """
    ray.get(controller.delete_backend.remote(backend_tag))


@_ensure_connected
def set_traffic(endpoint_name: str,
                traffic_policy_dictionary: Dict[str, float]) -> None:
    """Associate a service endpoint with traffic policy.

    Example:

    >>> serve.set_traffic("service-name", {
        "backend:v1": 0.5,
        "backend:v2": 0.5
    })

    Args:
        endpoint_name (str): A registered service endpoint.
        traffic_policy_dictionary (dict): a dictionary maps backend names
            to their traffic weights. The weights must sum to 1.
    """
    ray.get(
        controller.set_traffic.remote(endpoint_name,
                                      traffic_policy_dictionary))


@_ensure_connected
def shadow_traffic(endpoint_name: str, backend_tag: str,
                   proportion: float) -> None:
    """Shadow traffic from an endpoint to a backend.

    The specified proportion of requests will be duplicated and sent to the
    backend. Responses of the duplicated traffic will be ignored.
    The backend must not already be in use.

    To stop shadowing traffic to a backend, call `shadow_traffic` with
    proportion equal to 0.

    Args:
        endpoint_name (str): A registered service endpoint.
        backend_tag (str): A registered backend.
        proportion (float): The proportion of traffic from 0 to 1.
    """

    if not isinstance(proportion, (float, int)) or not 0 <= proportion <= 1:
        raise TypeError("proportion must be a float from 0 to 1.")

    ray.get(
        controller.shadow_traffic.remote(endpoint_name, backend_tag,
                                         proportion))


@_ensure_connected
def get_handle(endpoint_name: str, missing_ok: bool = False) -> RayServeHandle:
    """Retrieve RayServeHandle for service endpoint to invoke it from Python.

    Args:
        endpoint_name (str): A registered service endpoint.
        missing_ok (bool): If true, skip the check for the endpoint existence.
            It can be useful when the endpoint has not been registered.

    Returns:
        RayServeHandle
    """
    if not missing_ok:
        assert endpoint_name in ray.get(controller.get_all_endpoints.remote())

    # TODO(edoakes): we should choose the router on the same node.
    routers = ray.get(controller.get_routers.remote())
    return RayServeHandle(
        list(routers.values())[0],
        endpoint_name,
    )
