import mmh3

def bytes_to_str(data):
    return "".join(chr(c) for c in data)


def build_dt_group_id(group_id: str, namespace: str = ""):
    final_message = []

    # Add the namespace
    final_message.extend(bytes(namespace, encoding="utf-8"))

    # Convert the length of the namespace to Big Endian and add the last 4 bytes
    namespace_len_big_endian = len(namespace).to_bytes(8, byteorder="big", signed=False)
    final_message.extend(namespace_len_big_endian[-4:])

    # Add the group id
    final_message.extend(bytes(group_id, encoding="utf-8"))

    # Convert the length of the group_id to Big Endian and add the last 4 bytes
    group_id_len_big_endian = len(group_id).to_bytes(8, byteorder="big", signed=False)
    final_message.extend(group_id_len_big_endian[-4:])

    return bytes(final_message)


def dt_murmur3(data: bytes) -> str:
    return f"{int(mmh3.hash128(data, seed=0)):X}"[16:]


def build_dt_custom_device_id(group_id: str, custom_device_id: str):
    group_id = int(group_id, 16)

    final_message = []

    # Convert the length of the group_id to Big Endian and add the last 4 bytes
    group_id_len_big_endian = group_id.to_bytes(8, byteorder="big", signed=False)
    final_message.extend(group_id_len_big_endian[-8:])

    # Add the custom_device_id
    final_message.extend(bytes(custom_device_id, encoding="utf-8"))

    # Convert the length of the device_id to Big Endian and add the last 4 bytes
    device_id_len_big_endian = len(custom_device_id).to_bytes(8, byteorder="big", signed=False)
    final_message.extend(device_id_len_big_endian[-4:])

    return bytes(final_message)


def main():
    group_namespace = ""  # This is the default for extensions
    group_id = "My Group"
    custom_device_id = "My Device"

    dt_group_id = build_dt_group_id(group_id, group_namespace)
    print(
        f"""
        Original Group ID: '{group_id}'.
        After Dynatrace transformation: '{bytes_to_str(dt_group_id)}'.
        Final ID: 'CUSTOM_DEVICE_GROUP-{dt_murmur3(dt_group_id)}'
        """
    )

    dt_custom_device_id = build_dt_custom_device_id(dt_murmur3(dt_group_id), custom_device_id)
    print(
        f"""
       Original Custom Device ID: '{custom_device_id}'.
       After Dynatrace transformation: '{bytes_to_str(dt_custom_device_id)}'.
       Final ID: 'CUSTOM_DEVICE-{dt_murmur3(dt_custom_device_id)}'
       """
    )


if __name__ == "__main__":
    main()