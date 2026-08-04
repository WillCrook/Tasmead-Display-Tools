"""One-shot Qt worker for a debris simulation request."""

from __future__ import annotations

import threading
import traceback
from dataclasses import dataclass

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from services import (
    DebrisSimulationRequest,
    SimulationCancelled,
    run_debris_simulation_request,
)


@dataclass(frozen=True, slots=True)
class SimulationFailure:
    exception_type: str
    message: str
    traceback: str


class CancellationToken:
    """Thread-safe cooperative cancellation shared with a busy worker."""

    def __init__(self):
        self._event = threading.Event()

    def cancel(self):
        self._event.set()

    def is_cancelled(self):
        return self._event.is_set()


class DebrisSimulationWorker(QObject):
    progress = pyqtSignal(object)
    succeeded = pyqtSignal(object)
    cancelled = pyqtSignal()
    failed = pyqtSignal(object)
    finished = pyqtSignal()

    def __init__(self, request, cancellation_token, *, runner=None):
        super().__init__()
        if not isinstance(request, DebrisSimulationRequest):
            raise TypeError("request must be a DebrisSimulationRequest")
        self._request = request
        self._cancellation_token = cancellation_token
        self._runner = runner or run_debris_simulation_request
        self._ran = False

    @pyqtSlot()
    def run(self):
        if self._ran:
            return
        self._ran = True
        terminal_emitted = False
        try:
            result = self._runner(
                self._request,
                progress_callback=self.progress.emit,
                cancellation_check=self._cancellation_token.is_cancelled,
            )
        except SimulationCancelled:
            terminal_emitted = True
            self.cancelled.emit()
        except Exception as error:
            terminal_emitted = True
            self.failed.emit(
                SimulationFailure(
                    exception_type=error.__class__.__name__,
                    message=str(error) or error.__class__.__name__,
                    traceback=traceback.format_exc(),
                )
            )
        else:
            terminal_emitted = True
            self.succeeded.emit(result)
        finally:
            if not terminal_emitted:
                self.failed.emit(
                    SimulationFailure(
                        exception_type="WorkerTerminalError",
                        message="The simulation worker exited without a terminal result.",
                        traceback="",
                    )
                )
            self.finished.emit()
