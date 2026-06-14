"""Accessor layer: all external I/O (network and browser).

The only layer permitted to touch curl_cffi, Patchright, and nodriver. It depends
inward on ``core`` for types/config/helpers and is consumed by ``service``. A
future ONNX page-state classifier's model loader (local file I/O) would also live
here, with the pure inference call kept in ``core.detection``.
"""
