import argparse
import multiprocessing
import os
import threading
import time
from typing import Any, Tuple, Union, Dict

from .helper import _get_event, ConditionalEvent
from ... import __stop_msg__, __ready_msg__, __default_host__, __docker_host__
from ...enums import PeaRoleType, RuntimeBackendType, SocketType, GatewayProtocolType
from ...excepts import RuntimeFailToStart, RuntimeRunForeverEarlyError
from ...helper import typename
from ...hubble.helper import is_valid_huburi
from ...hubble.hubio import HubIO
from ...logging.logger import JinaLogger
from ...parsers.hubble import set_hub_pull_parser

__all__ = ['BasePea']


def run(
    args: 'argparse.Namespace',
    name: str,
    runtime_cls,
    envs: Dict[str, str],
    timeout_ctrl: int,
    zed_runtime_ctrl_address: str,
    is_started: Union['multiprocessing.Event', 'threading.Event'],
    is_shutdown: Union['multiprocessing.Event', 'threading.Event'],
    is_ready: Union['multiprocessing.Event', 'threading.Event'],
    cancel_event: Union['multiprocessing.Event', 'threading.Event'],
):
    """Method representing the :class:`BaseRuntime` activity.

    This method is the target for the Pea's `thread` or `process`

    .. note::
        :meth:`run` is running in subprocess/thread, the exception can not be propagated to the main process.
        Hence, please do not raise any exception here.

    .. note::
        Please note that env variables are process-specific. Subprocess inherits envs from
        the main process. But Subprocess's envs do NOT affect the main process. It does NOT
        mess up user local system envs.

    .. warning::
        If you are using ``thread`` as backend, envs setting will likely be overidden by others

    :param args: namespace args from the Pea
    :param name: name of the Pea to have proper logging
    :param runtime_cls: the runtime class to instantiate
    :param envs: a dictionary of environment variables to be set in the new Process
    :param timeout_ctrl: timeout time for the control port communication
    :param zed_runtime_ctrl_address: the control address of the `ZEDRuntime` that is supported by the Pea or `ContainerRuntime` or `JinadRuntime`.
    :param is_started: concurrency event to communicate runtime is properly started. Used for better logging
    :param is_shutdown: concurrency event to communicate runtime is terminated
    :param is_ready: concurrency event to communicate runtime is ready to receive messages
    :param cancel_event: concurrency event to receive cancelling signal from the Pea. Needed by some runtimes
    """
    logger = JinaLogger(name, **vars(args))

    def _unset_envs():
        if envs and args.runtime_backend != RuntimeBackendType.THREAD:
            for k in envs.keys():
                os.unsetenv(k)

    def _set_envs():
        if args.env:
            if args.runtime_backend == RuntimeBackendType.THREAD:
                logger.warning(
                    'environment variables should not be set when runtime="thread".'
                )
            else:
                os.environ.update({k: str(v) for k, v in envs.items()})

    try:
        _set_envs()
        runtime = runtime_cls(
            args=args,
            ctrl_addr=zed_runtime_ctrl_address,
            ready_event=is_ready,
            cancel_event=cancel_event,
            timeout_ctrl=timeout_ctrl,
        )
    except Exception as ex:
        logger.error(
            f'{ex!r} during {runtime_cls!r} initialization'
            + f'\n add "--quiet-error" to suppress the exception details'
            if not args.quiet_error
            else '',
            exc_info=not args.quiet_error,
        )
    else:
        is_started.set()
        with runtime:
            runtime.run_forever()
    finally:
        _unset_envs()
        is_shutdown.set()


class BasePea:
    """
    :class:`BasePea` is a thread/process- container of :class:`BaseRuntime`. It leverages :class:`threading.Thread`
    or :class:`multiprocessing.Process` to manage the lifecycle of a :class:`BaseRuntime` object in a robust way.

    A :class:`BasePea` must be equipped with a proper :class:`Runtime` class to work.
    """

    def __init__(self, args: 'argparse.Namespace'):
        super().__init__()  #: required here to call process/thread __init__
        self.args = args
        self.name = self.args.name or self.__class__.__name__

        self.logger = JinaLogger(self.name, **vars(self.args))

        if self.args.runtime_backend == RuntimeBackendType.THREAD:
            self.logger.warning(
                f' Using Thread as runtime backend is not recommended for production purposes. It is '
                f'just supposed to be used for easier debugging. Besides the performance considerations, it is'
                f'specially dangerous to mix `Executors` running in different types of `RuntimeBackends`.'
            )

        self._envs = {'JINA_POD_NAME': self.name, 'JINA_LOG_ID': self.args.identity}
        if self.args.quiet:
            self._envs['JINA_LOG_CONFIG'] = 'QUIET'
        if self.args.env:
            self._envs.update(self.args.env)

        # arguments needed to create `runtime` and communicate with it in the `run` in the stack of the new process
        # or thread. Control address from Zmqlet has some randomness and therefore we need to make sure Pea knows
        # control address of runtime
        self.runtime_cls = self._get_runtime_cls()
        self._timeout_ctrl = self.args.timeout_ctrl
        self._set_ctrl_adrr()
        test_worker = {
            RuntimeBackendType.THREAD: threading.Thread,
            RuntimeBackendType.PROCESS: multiprocessing.Process,
        }.get(getattr(args, 'runtime_backend', RuntimeBackendType.THREAD))()
        self.is_ready = _get_event(test_worker)
        self.is_shutdown = _get_event(test_worker)
        self.cancel_event = _get_event(test_worker)
        self.is_started = _get_event(test_worker)
        self.ready_or_shutdown = ConditionalEvent(
            getattr(args, 'runtime_backend', RuntimeBackendType.THREAD),
            events_list=[self.is_ready, self.is_shutdown],
        )
        self.worker = {
            RuntimeBackendType.THREAD: threading.Thread,
            RuntimeBackendType.PROCESS: multiprocessing.Process,
        }.get(getattr(args, 'runtime_backend', RuntimeBackendType.THREAD))(
            target=run,
            kwargs={
                'args': args,
                'name': self.name,
                'envs': self._envs,
                'is_started': self.is_started,
                'is_shutdown': self.is_shutdown,
                'is_ready': self.is_ready,
                'cancel_event': self.cancel_event,
                'timeout_ctrl': self._timeout_ctrl,
                'zed_runtime_ctrl_address': self._zed_runtime_ctrl_address,
                'runtime_cls': self.runtime_cls,
            },
        )
        self.daemon = self.args.daemon  #: required here to set process/thread daemon

    def _set_ctrl_adrr(self):
        """Sets control address for different runtimes"""
        # This logic must be improved specially when it comes to naming. It is about relative local/remote position
        # between the runtime and the `ZEDRuntime` it may control
        from ..zmq import Zmqlet
        from ..runtimes.container import ContainerRuntime

        if self.runtime_cls == ContainerRuntime:
            # Checks if caller (JinaD) has set `docker_kwargs['extra_hosts']` to __docker_host__.
            # If yes, set host_ctrl to __docker_host__, else keep it as __default_host__
            # Reset extra_hosts as that's set by default in ContainerRuntime
            if (
                self.args.docker_kwargs
                and 'extra_hosts' in self.args.docker_kwargs
                and __docker_host__ in self.args.docker_kwargs['extra_hosts']
            ):
                ctrl_host = __docker_host__
                self.args.docker_kwargs.pop('extra_hosts')
            else:
                ctrl_host = self.args.host

            self._zed_runtime_ctrl_address = Zmqlet.get_ctrl_address(
                ctrl_host, self.args.port_ctrl, self.args.ctrl_with_ipc
            )[0]
        else:
            self._zed_runtime_ctrl_address = Zmqlet.get_ctrl_address(
                self.args.host, self.args.port_ctrl, self.args.ctrl_with_ipc
            )[0]

    def start(self):
        """Start the Pea.
        This method calls :meth:`start` in :class:`threading.Thread` or :class:`multiprocesssing.Process`.
        .. #noqa: DAR201
        """
        self.worker.start()
        if not self.args.noblock_on_start:
            self.wait_start_success()
        return self

    def join(self, *args, **kwargs):
        """Joins the Pea.
        This method calls :meth:`join` in :class:`threading.Thread` or :class:`multiprocesssing.Process`.

        :param args: extra positional arguments to pass to join
        :param kwargs: extra keyword arguments to pass to join
        """
        self.worker.join(*args, **kwargs)

    def terminate(self):
        """Terminate the Pea.
        This method calls :meth:`terminate` in :class:`threading.Thread` or :class:`multiprocesssing.Process`.
        """
        if hasattr(self.worker, 'terminate'):
            self.worker.terminate()

    def _retry_control_message(self, command: str, num_retry: int = 3):
        from ..zmq import send_ctrl_message

        for retry in range(1, num_retry + 1):
            self.logger.debug(f'Sending {command} command for the {retry}th time')
            try:
                send_ctrl_message(
                    self._zed_runtime_ctrl_address,
                    command,
                    timeout=self._timeout_ctrl,
                    raise_exception=True,
                )
                break
            except Exception as ex:
                self.logger.warning(f'{ex!r}')
                if retry == num_retry:
                    raise ex

    def activate_runtime(self):
        """ Send activate control message. """
        if self._is_dealer:
            self._retry_control_message('ACTIVATE')

    def _deactivate_runtime(self):
        """Send deactivate control message. """
        if self._is_dealer:
            self._retry_control_message('DEACTIVATE')

    def _cancel_runtime(self):
        """Send terminate control message."""
        from ..runtimes.zmq.zed import ZEDRuntime
        from ..runtimes.container import ContainerRuntime

        if self.runtime_cls == ZEDRuntime or self.runtime_cls == ContainerRuntime:
            self._retry_control_message('TERMINATE')
        else:
            self.cancel_event.set()

    def wait_start_success(self):
        """Block until all peas starts successfully.

        If not success, it will raise an error hoping the outer function to catch it
        """
        _timeout = self.args.timeout_ready
        if _timeout <= 0:
            _timeout = None
        else:
            _timeout /= 1e3
        self.logger.debug('waiting for ready or shutdown signal from runtime')
        if self.ready_or_shutdown.event.wait(_timeout):
            if self.is_shutdown.is_set():
                # return too early and the shutdown is set, means something fails!!
                if not self.is_started.is_set():
                    raise RuntimeFailToStart
                else:
                    raise RuntimeRunForeverEarlyError
            else:
                self.logger.success(__ready_msg__)
        else:
            _timeout = _timeout or -1
            self.logger.warning(
                f'{self.runtime_cls!r} timeout after waiting for {self.args.timeout_ready}ms, '
                f'if your executor takes time to load, you may increase --timeout-ready'
            )
            self.close()
            raise TimeoutError(
                f'{typename(self)}:{self.name} can not be initialized after {_timeout * 1e3}ms'
            )

    @property
    def _is_dealer(self):
        """Return true if this `Pea` must act as a Dealer responding to a Router
        .. # noqa: DAR201
        """
        return self.args.socket_in == SocketType.DEALER_CONNECT

    def close(self) -> None:
        """Close the Pea

        This method makes sure that the `Process/thread` is properly finished and its resources properly released
        """
        # if that 1s is not enough, it means the process/thread is still in forever loop, cancel it
        self.logger.debug('waiting for ready or shutdown signal from runtime')
        if self.is_ready.is_set() and not self.is_shutdown.is_set():
            try:
                self._deactivate_runtime()
                self._cancel_runtime()
                if not self.is_shutdown.wait(timeout=self._timeout_ctrl):
                    self.terminate()
                    time.sleep(0.1)
                    raise Exception(
                        f'Shutdown signal was not received for {self._timeout_ctrl}'
                    )
            except Exception as ex:
                self.logger.error(
                    f'{ex!r} during {self.close!r}'
                    + f'\n add "--quiet-error" to suppress the exception details'
                    if not self.args.quiet_error
                    else '',
                    exc_info=not self.args.quiet_error,
                )

            # if it is not daemon, block until the process/thread finish work
            if not self.args.daemon:
                self.join()
        elif self.is_shutdown.is_set():
            # here shutdown has been set already, therefore `run` will gracefully finish
            pass
        else:
            # sometimes, we arrive to the close logic before the `is_ready` is even set.
            # Observed with `gateway` when Pods fail to start
            self.logger.warning(
                'Pea is being closed before being ready. Most likely some other Pea in the Flow or Pod '
                'failed to start'
            )
            _timeout = self.args.timeout_ready
            if _timeout <= 0:
                _timeout = None
            else:
                _timeout /= 1e3
            self.logger.debug('waiting for ready or shutdown signal from runtime')
            if self.ready_or_shutdown.event.wait(_timeout):
                if self.is_ready.is_set():
                    self._cancel_runtime()
                    if not self.is_shutdown.wait(timeout=self._timeout_ctrl):
                        self.terminate()
                        time.sleep(0.1)
                        raise Exception(
                            f'Shutdown signal was not received for {self._timeout_ctrl}'
                        )
            else:
                self.logger.warning(
                    'Terminating process after waiting for readiness signal for graceful shutdown'
                )
                # Just last resource, terminate it
                self.terminate()
                time.sleep(0.1)
        self.logger.debug(__stop_msg__)
        self.logger.close()

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def _get_runtime_cls(self) -> Tuple[Any, bool]:
        gateway_runtime_dict = {
            GatewayProtocolType.GRPC: 'GRPCRuntime',
            GatewayProtocolType.WEBSOCKET: 'WebSocketRuntime',
            GatewayProtocolType.HTTP: 'HTTPRuntime',
        }
        if (
            self.args.runtime_cls not in gateway_runtime_dict.values()
            and self.args.host != __default_host__
            and not self.args.disable_remote
        ):
            self.args.runtime_cls = 'JinadRuntime'
            # NOTE: remote pea would also create a remote workspace which might take alot of time.
            # setting it to -1 so that wait_start_success doesn't fail
            self.args.timeout_ready = -1
        if self.args.runtime_cls == 'ZEDRuntime' and self.args.uses.startswith(
            'docker://'
        ):
            self.args.runtime_cls = 'ContainerRuntime'
        if self.args.runtime_cls == 'ZEDRuntime' and is_valid_huburi(self.args.uses):
            self.args.uses = HubIO(
                set_hub_pull_parser().parse_args([self.args.uses, '--no-usage'])
            ).pull()
            if self.args.uses.startswith('docker://'):
                self.args.runtime_cls = 'ContainerRuntime'
        if hasattr(self.args, 'protocol'):
            self.args.runtime_cls = gateway_runtime_dict[self.args.protocol]
        from ..runtimes import get_runtime

        return get_runtime(self.args.runtime_cls)

    @property
    def role(self) -> 'PeaRoleType':
        """Get the role of this pea in a pod


        .. #noqa: DAR201"""
        return self.args.pea_role

    @property
    def _is_inner_pea(self) -> bool:
        """Determine whether this is a inner pea or a head/tail


        .. #noqa: DAR201"""
        return self.role is PeaRoleType.SINGLETON or self.role is PeaRoleType.PARALLEL
