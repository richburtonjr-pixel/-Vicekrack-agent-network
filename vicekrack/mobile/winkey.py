"""Keep the xAI key in Windows Credential Manager (encrypted for your Windows login), so ViceKrack can start
by itself after a restart. The key is never written to a file, printed or sent to the phone.

Uses the Windows API through ctypes (no extra packages). On other systems these raise `unsupported`.
"""

import ctypes
import sys

from ..errors import NetworkError

TARGET = "ViceKrack/XAI_API_KEY"
CRED_TYPE_GENERIC, CRED_PERSIST_LOCAL_MACHINE = 1, 2
ERROR_NOT_FOUND = 1168


def _api():
    if sys.platform != "win32":
        raise NetworkError("unsupported", "Saving the key is only available on Windows.")
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                    ("Comment", wintypes.LPWSTR), ("LastWritten", FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                    ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
                    ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                    ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
    advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                 ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
    advapi.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    advapi.CredFree.argtypes = [ctypes.c_void_p]
    return advapi, CREDENTIAL


def save_named(target, value, *, label="key"):
    """Store one secret under a Credential Manager target name. Values are never returned or printed."""
    if not target.startswith("ViceKrack/") or not value or len(value) > 512 or any(ch.isspace() for ch in value):
        raise NetworkError("invalid_key", f"That does not look like a valid {label}.")
    advapi, CREDENTIAL = _api()
    blob = value.encode("utf-16-le")
    buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    credential = CREDENTIAL(Type=CRED_TYPE_GENERIC, TargetName=target, CredentialBlobSize=len(blob),
                            CredentialBlob=ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
                            Persist=CRED_PERSIST_LOCAL_MACHINE, UserName="vicekrack")
    if not advapi.CredWriteW(ctypes.byref(credential), 0):
        raise NetworkError("key_not_saved", f"Windows did not save the {label}.")


def load_named(target):
    """The stored value for a target, or None if none is saved."""
    advapi, CREDENTIAL = _api()
    pointer = ctypes.POINTER(CREDENTIAL)()
    if not advapi.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
        if ctypes.get_last_error() == ERROR_NOT_FOUND:
            return None
        raise NetworkError("key_not_read", "Windows could not read the saved key.")
    try:
        size = pointer.contents.CredentialBlobSize
        return ctypes.string_at(pointer.contents.CredentialBlob, size).decode("utf-16-le")
    finally:
        advapi.CredFree(pointer)


def save_key(key):
    key = (key or "").strip()
    if not key:
        raise NetworkError("invalid_key", "That does not look like an xAI API key.")
    save_named(TARGET, key, label="xAI API key")


def load_key():
    """The saved key, or None if none is saved."""
    return load_named(TARGET)


def delete_key():
    advapi, _ = _api()
    if not advapi.CredDeleteW(TARGET, CRED_TYPE_GENERIC, 0) and ctypes.get_last_error() != ERROR_NOT_FOUND:
        raise NetworkError("key_not_deleted", "Windows could not remove the saved key.")
