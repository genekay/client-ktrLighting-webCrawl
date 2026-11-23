# -*- coding: utf-8 -*-
"""Retry transient Zyte API errors (website-connection / temporary bans).

The portal refuses connections (421) and returns temporary download errors
(520/521) under load. These are transient, so retry them several times with
backoff instead of letting them surface as exceptions.
"""
from tenacity import retry_if_exception, stop_after_attempt

try:  # python-zyte-api >= 0.5 exposes these at top level
    from zyte_api import RetryFactory, RequestError
except ImportError:  # older layout
    from zyte_api.aio.retry import RetryFactory
    from zyte_api.aio.errors import RequestError

_TRANSIENT = frozenset({421, 429, 500, 503, 520, 521})


def _is_transient(exc):
    return isinstance(exc, RequestError) and exc.status in _TRANSIENT


class _Factory(RetryFactory):
    retry_condition = RetryFactory.retry_condition | retry_if_exception(_is_transient)
    temporary_download_error_stop = stop_after_attempt(6)

    def stop(self, retry_state):
        if _is_transient(retry_state.outcome.exception()):
            return self.temporary_download_error_stop(retry_state)
        return super().stop(retry_state)

    def wait(self, retry_state):
        if _is_transient(retry_state.outcome.exception()):
            return self.temporary_download_error_wait(retry_state=retry_state)
        return super().wait(retry_state)


RETRY_POLICY = _Factory().build()
