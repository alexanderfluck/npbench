import subprocess
from pathlib import Path
import argparse

def check_sudo_privileges():
    """Check if the current user has sudo privileges by running a simple sudo command."""
    try:
        subprocess.run(['sudo', '-v'], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return True
    except subprocess.CalledProcessError:
        return False

def get_memory_info(file_path:Path=""):
    print(file_path)
    if file_path:
        print("filepath true")         
        dmi_decode_text = ""
        devices = []
        try:
            with open(file_path, "r") as f:
                dmi_decode_text = f.read()
        except:
            print("Could not read file")
            return {}
        
        blocks = dmi_decode_text.strip().split('\n\n')
        for block in blocks:
            block_lines = block.split('\n')    
            if "DMI type 17" not in block_lines[0]:
                continue
            else:
                header = block_lines[0:2]
                block_lines = block_lines[2:]

                handle, dmi_type, structure_size = list(map(lambda x: x.strip(), header[0].split(',')))
                type_name = header[1].strip() 

                block_dict = {'Handle': handle, 'Type number': dmi_type, 'Type name': type_name, 'Structure size': structure_size}
                for block_line in block_lines:
                    key, value = block_line.split(":", 1)
                    block_dict[key.strip()] = value.strip()
                devices.append(block_dict)
        return devices
    elif not check_sudo_privileges():
        print("Cannot get memory information. The memory info is based on the dmidecode command and thus needs sudo privileges.")
        return None
    else:
        result = subprocess.run(['sudo', 'dmidecode', '--type', '17'], capture_output=True, text=True)
        # Split the output into blocks for each memory device
        blocks = result.stdout.strip().split('\n\n')
        blocks = blocks[1:]
        devices = []
        for block in blocks:
            block_lines = block.split('\n')
            header = block_lines[0:2]
            block_lines = block_lines[2:]

            handle, dmi_type, structure_size = list(map(lambda x: x.strip(), header[0].split(',')))
            type_name = header[1].strip() 

            block_dict = {'Handle': handle, 'Type number': dmi_type, 'Type name': type_name, 'Structure size': structure_size}
            for block_line in block_lines:
                key, value = block_line.split(":", 1)
                block_dict[key.strip()] = value.strip()
            devices.append(block_dict)
        return devices

def get_theoretical_bandwidth(file_path:str = ""):
    """returns the theoretical bandwidth in MB/s"""
    mem_info = get_memory_info(file_path)
    if not mem_info:
        return 0
    used_channels = set()
    speed_mt = 0
    width = 0
    for device in mem_info:
        if device.get("Size", "No Module Installed") != "No Module Installed":
            channel = device.get("Bank Locator", "Unknown Channel") + "_" +device.get("Locator", "unknown_slot")
            used_channels.add(channel)
            device_speed = int(device.get("Configured Memory Speed", "0").split()[0])
            if speed_mt == 0:
                speed_mt = device_speed
            elif speed_mt != device_speed:
                raise NotImplementedError("The function for calculating theoretical bandwidth has not been designed to support multiple different memory speeds")

            device_width = int(device.get("Data Width", "0").split()[0])/8
            if width == 0:
                width = device_width
            elif width != device_width:
                raise NotImplementedError("The function for calculating theoretical bandwidth has not been designed to support multiple different memory widths")
    print(speed_mt, width, used_channels)
    return speed_mt * width * len(used_channels)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-f",
                        "--filepath",
                        type=str,
                        nargs="?",
                        default="")
    args = vars(parser.parse_args())
    filepath = args["filepath"]
    
    print(get_theoretical_bandwidth(filepath), "MB/s")