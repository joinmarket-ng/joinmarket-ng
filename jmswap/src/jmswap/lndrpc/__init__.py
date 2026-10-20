"""Generated gRPC bindings for the pinned LND ChannelEscrow service.

The modules in this package are produced by ``protoc``; do not edit them.
Regenerate them from the pinned proto (``jmswap/proto/channelescrow.proto``,
copied verbatim from ``lnrpc/channelescrowrpc/channelescrow.proto`` of the LND
build in ``lnd/``) with::

    python -m grpc_tools.protoc -Ijmswap/proto \
        --python_out=jmswap/src/jmswap/lndrpc \
        --pyi_out=jmswap/src/jmswap/lndrpc \
        --grpc_python_out=jmswap/src/jmswap/lndrpc \
        jmswap/proto/channelescrow.proto

``protoc`` emits a top-level ``import channelescrow_pb2``; rewrite that single
line in ``channelescrow_pb2_grpc.py`` to ``from . import channelescrow_pb2`` so
the package stays importable without manipulating ``sys.path``.

The other modules are generated the same way from the protos of the same LND
release (``lightning.proto``, ``router.proto`` and ``invoices.proto`` from
``lnrpc/``, ``lnrpc/routerrpc/`` and ``lnrpc/invoicesrpc/``) with the
``grpcio-tools`` version pinned in ``requirements-dev.txt``. Rewrite their
top-level ``import <name>_pb2`` lines to relative imports the same way.
"""
