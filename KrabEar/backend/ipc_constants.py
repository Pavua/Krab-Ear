"""IPC (Inter-Process Communication) constants for Krab Ear backend.

Defines magic numbers for Unix socket server configuration, buffer sizes,
timeouts, and other IPC protocol parameters.
"""

# Socket server configuration
IPC_SOCKET_BACKLOG = 32  # listen() queue size for pending connections
IPC_SOCKET_TIMEOUT_SEC = 0.8  # socket.settimeout() for accept() loop polling

# Buffer and message size limits
IPC_MAX_MESSAGE_BYTES = 1024 * 1024  # 1 MB max message size for recv()

# Connection handling timeouts
IPC_PREVIEW_THREAD_TIMEOUT_SEC = 1.5  # timeout for preview thread join()

# Socket file permissions
IPC_SOCKET_PERMISSIONS = 0o600  # read/write owner only for Unix socket
