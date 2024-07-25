import hashlib
from typing import Optional, Tuple, Union

from pymmh3 import hash128


def dt_group_and_device_ids(group_name: str, device_name: str, namespace: Optional[str] = "") -> Tuple[str, str]:
    """
    Calculates the Group ID and Custom Device ID
    """
    group_id = dt_murmur3(build_dt_group_id(group_name, namespace))
    custom_device_id = dt_murmur3(build_dt_custom_device_id(group_id, device_name))

    return f"CUSTOM_DEVICE_GROUP-{group_id}", f"CUSTOM_DEVICE-{custom_device_id}"


def dt_murmur3(data: bytes) -> str:
    return f"{int(hash128(data, seed=0)):X}"[-16:]


def build_dt_custom_device_id(group_id: Union[str, bytes], custom_device_name: str):

    group_id = int(group_id, 16)

    final_message = []

    # Convert the length of the group_id to Big Endian and add the last 4 bytes
    group_id_len_big_endian = group_id.to_bytes(8, byteorder="big", signed=False)
    final_message.extend(group_id_len_big_endian[-8:])

    # Add the custom_device_id
    final_message.extend(bytes(custom_device_name, encoding="utf-8"))

    # Convert the length of the device_id to Big Endian and add the last 4 bytes
    device_id_len_big_endian = len(custom_device_name).to_bytes(8, byteorder="big", signed=False)
    final_message.extend(device_id_len_big_endian[-4:])

    return bytes(final_message)


def build_dt_group_id(group_name: Union[str, bytes], namespace: str = ""):
    final_message = []

    # Add the namespace
    final_message.extend(bytes(namespace, encoding="utf-8"))

    # Convert the length of the namespace to Big Endian and add the last 4 bytes
    namespace_len_big_endian = len(namespace).to_bytes(8, byteorder="big", signed=False)
    final_message.extend(namespace_len_big_endian[-4:])

    # Add the group id
    final_message.extend(bytes(group_name, encoding="utf-8"))

    # Convert the length of the group_id to Big Endian and add the last 4 bytes
    group_id_len_big_endian = len(group_name).to_bytes(8, byteorder="big", signed=False)
    final_message.extend(group_id_len_big_endian[-4:])

    return bytes(final_message)
