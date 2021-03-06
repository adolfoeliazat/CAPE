# CAPE - Config And Payload Extraction
# Copyright(C) 2015, 2016 Context Information Security. (kevin.oreilly@contextis.com)
# 
# This program is free software : you can redistribute it and / or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with this program.If not, see <http://www.gnu.org/licenses/>.

import struct
from lib.cuckoo.common.abstracts import Signature

IMAGE_DOS_SIGNATURE             = 0x5A4D
IMAGE_NT_SIGNATURE              = 0x00004550
OPTIONAL_HEADER_MAGIC_PE        = 0x10b
OPTIONAL_HEADER_MAGIC_PE_PLUS   = 0x20b
IMAGE_FILE_EXECUTABLE_IMAGE     = 0x0002
PE_HEADER_LIMIT                 = 0x200

EXECUTABLE_FLAGS                = 0x10 | 0x20 | 0x40 | 0x80
EXTRACTION_MIN_SIZE             = 0x1001

PLUGX_SIGNATURE		            = 0x5658

class CAPE_Compression(Signature):
    name = "Compression"
    description = "CAPE detection: Compression (or decompression)"
    severity = 1
    categories = ["malware"]
    authors = ["kevoreilly"]
    minimum = "1.3"
    evented = True

    filter_apinames = set(["RtlDecompressBuffer"])

    def __init__(self, *args, **kwargs):
        Signature.__init__(self, *args, **kwargs)
        self.compressed_binary = False

    def on_call(self, call, process):
        if call["api"] == "RtlDecompressBuffer":
            buf = self.get_raw_argument(call, "UncompressedBuffer")
            dos_header = buf[:64]

            if struct.unpack("<H", dos_header[0:2])[0] == IMAGE_DOS_SIGNATURE:
                self.compressed_binary = True

            # Check for sane value in e_lfanew
            e_lfanew, = struct.unpack("<L", dos_header[60:64])
            if not e_lfanew or e_lfanew > PE_HEADER_LIMIT:
                return
            
            nt_headers = buf[e_lfanew:e_lfanew+256]

            #if ((pNtHeader->FileHeader.Machine == 0) || (pNtHeader->FileHeader.SizeOfOptionalHeader == 0 || pNtHeader->OptionalHeader.SizeOfHeaders == 0)) 
            if struct.unpack("<H", nt_headers[4:6]) == 0 or struct.unpack("<H", nt_headers[20:22]) == 0 or struct.unpack("<H", nt_headers[84:86]) == 0:
                return

            #if (!(pNtHeader->FileHeader.Characteristics & IMAGE_FILE_EXECUTABLE_IMAGE)) 
            if (struct.unpack("<H", nt_headers[22:24])[0] & IMAGE_FILE_EXECUTABLE_IMAGE) == 0:
                return

            #if (pNtHeader->FileHeader.SizeOfOptionalHeader & (sizeof (ULONG_PTR) - 1)) 
            if struct.unpack("<H", nt_headers[20:22])[0] & 3 != 0:
                return

            #if ((pNtHeader->OptionalHeader.Magic != IMAGE_NT_OPTIONAL_HDR32_MAGIC) && (pNtHeader->OptionalHeader.Magic != IMAGE_NT_OPTIONAL_HDR64_MAGIC))
            if struct.unpack("<H", nt_headers[24:26])[0] != OPTIONAL_HEADER_MAGIC_PE and struct.unpack("<H", nt_headers[24:26])[0] != OPTIONAL_HEADER_MAGIC_PE_PLUS:
                return

            # To pass the above tests it should now be safe to assume it's a PE image
            self.compressed_binary = True            
            
    def on_complete(self):
        if self.compressed_binary == True:
            return True

class CAPE_Extraction(Signature):
    name = "Extraction"
    description = "CAPE detection: Executable code extraction"
    severity = 1
    categories = ["allocation"]
    authors = ["kevoreilly"]
    minimum = "1.3"
    evented = True
    
    def __init__(self, *args, **kwargs):
        Signature.__init__(self, *args, **kwargs)

    filter_apinames = set(["NtAllocateVirtualMemory","NtProtectVirtualMemory","VirtualProtectEx"])

    def on_call(self, call, process):
    
        if process["process_name"] == "WINWORD.EXE":
            return False
        if call["api"] == "NtAllocateVirtualMemory":
            protection = int(self.get_raw_argument(call, "Protection"), 0)
            regionsize = int(self.get_raw_argument(call, "RegionSize"), 0)
            handle = self.get_argument(call, "ProcessHandle")
            if handle == "0xffffffff" and protection & EXECUTABLE_FLAGS and regionsize >= EXTRACTION_MIN_SIZE:
                return True
        if call["api"] == "VirtualProtectEx":
            protection = int(self.get_raw_argument(call, "Protection"), 0)
            size = int(self.get_raw_argument(call, "Size"), 0)
            handle = self.get_argument(call, "ProcessHandle")
            if handle == "0xffffffff" and protection & EXECUTABLE_FLAGS and size >= EXTRACTION_MIN_SIZE:
                return True
        elif call["api"] == "NtProtectVirtualMemory":
            protection = int(self.get_raw_argument(call, "NewAccessProtection"), 0)
            size = int(self.get_raw_argument(call, "NumberOfBytesProtected"), 0)
            handle = self.get_argument(call, "ProcessHandle")
            if handle == "0xffffffff" and protection & EXECUTABLE_FLAGS and size >= EXTRACTION_MIN_SIZE:
                return True

class CAPE_InjectionCreateRemoteThread(Signature):
    name = "InjectionCreateRemoteThread"
    description = "CAPE detection: Injection with CreateRemoteThread in a remote process"
    severity = 1
    categories = ["injection"]
    authors = ["JoseMi Holguin", "nex", "Optiv", "kevoreilly", "KillerInstinct"]
    minimum = "1.3"
    evented = True

    def __init__(self, *args, **kwargs):
        Signature.__init__(self, *args, **kwargs)
        self.lastprocess = None

    filter_categories = set(["process","threading"])

    def on_call(self, call, process):
        if process is not self.lastprocess:
            self.sequence = 0
            self.process_handles = set()
            self.process_pids = set()
            self.lastprocess = process

        if call["api"] == "OpenProcess" and call["status"] == True:
            if self.get_argument(call, "ProcessId") != process["process_id"]:
                self.process_handles.add(call["return"])
                self.process_pids.add(self.get_argument(call, "ProcessId"))
        elif call["api"] == "NtOpenProcess" and call["status"] == True:
            if self.get_argument(call, "ProcessIdentifier") != process["process_id"]:
                self.process_handles.add(self.get_argument(call, "ProcessHandle"))
                self.process_pids.add(self.get_argument(call, "ProcessIdentifier"))
        elif call["api"] == "CreateProcessInternalW":
            if self.get_argument(call, "ProcessId") != process["process_id"]:
                self.process_handles.add(self.get_argument(call, "ProcessHandle"))
                self.process_pids.add(self.get_argument(call, "ProcessId"))
        elif (call["api"] == "NtMapViewOfSection") and self.sequence == 0:
            if self.get_argument(call, "ProcessHandle") in self.process_handles:
                self.sequence = 2
        elif (call["api"] == "VirtualAllocEx" or call["api"] == "NtAllocateVirtualMemory") and self.sequence == 0:
            if self.get_argument(call, "ProcessHandle") in self.process_handles:
                self.sequence = 1
        elif (call["api"] == "NtWriteVirtualMemory" or call["api"] == "NtWow64WriteVirtualMemory64" or call["api"] == "WriteProcessMemory") and self.sequence == 1:
            if self.get_argument(call, "ProcessHandle") in self.process_handles:
                self.sequence = 2
        elif (call["api"] == "NtWriteVirtualMemory" or call["api"] == "NtWow64WriteVirtualMemory64"  or call["api"] == "WriteProcessMemory") and self.sequence == 2:
            if self.get_argument(call, "ProcessHandle") in self.process_handles:
                addr = int(self.get_argument(call, "BaseAddress"), 16)
                buf = self.get_argument(call, "Buffer")
                if addr >= 0x7c900000 and addr < 0x80000000 and buf.startswith("\\xe9"):
                    self.description = "Code injection via WriteProcessMemory-modified NTDLL code in a remote process"
                    #procname = self.get_name_from_pid(self.handle_map[handle])
                    #desc = "{0}({1}) -> {2}({3})".format(process["process_name"], str(process["process_id"]),
                    #                                     procname, self.handle_map[handle])
                    self.data.append({"Injection": desc})
                    return True
        elif (call["api"] == "CreateRemoteThread" or call["api"].startswith("NtCreateThread")) and self.sequence == 2:
            handle = self.get_argument(call, "ProcessHandle")
            if handle in self.process_handles:
                #procname = self.get_name_from_pid(self.handle_map[handle])
                #desc = "{0}({1}) -> {2}({3})".format(process["process_name"], str(process["process_id"]),
                #                                     procname, self.handle_map[handle])
                #self.data.append({"Injection": desc})
                return True
        elif call["api"].startswith("NtQueueApcThread") and self.sequence == 2:
            if str(self.get_argument(call, "ProcessId")) in self.process_pids:
                #self.description = "Code injection with NtQueueApcThread in a remote process"
                #desc = "{0}({1}) -> {2}({3})".format(self.lastprocess["process_name"], str(self.lastprocess["process_id"]),
                #                                     process["process_name"], str(process["process_id"]))
                #self.data.append({"Injection": desc})
                return True

class CAPE_InjectionProcessHollowing(Signature):
    name = "InjectionProcessHollowing"
    description = "CAPE detection: Injection (Process Hollowing)"
    severity = 1
    categories = ["injection"]
    authors = ["glysbaysb", "Optiv", "KillerInstinct"]
    minimum = "1.3"
    evented = True

    def __init__(self, *args, **kwargs):
        Signature.__init__(self, *args, **kwargs)
        self.lastprocess = None

    filter_categories = set(["process","threading"])

    def on_call(self, call, process):
        if process is not self.lastprocess:
            self.sequence = 0
            # technically we should have a separate state machine for each created process, but since this
            # code doesn't deal with handles properly as it is, this is sufficient
            self.process_handles = set()
            self.thread_handles = set()
            self.process_map = dict()
            self.thread_map = dict()
            self.lastprocess = process

        if call["api"] == "CreateProcessInternalW":
            phandle = self.get_argument(call, "ProcessHandle")
            thandle = self.get_argument(call, "ThreadHandle")
            pid = self.get_argument(call, "ProcessId")
            self.process_handles.add(phandle)
            self.process_map[phandle] = pid
            self.thread_handles.add(thandle)
            self.thread_map[thandle] = pid
        elif (call["api"] == "NtUnmapViewOfSection" or call["api"] == "NtAllocateVirtualMemory") and self.sequence == 0:
            if self.get_argument(call, "ProcessHandle") in self.process_handles:
                self.sequence = 1
        elif call["api"] == "NtGetContextThread" and self.sequence == 0:
           if self.get_argument(call, "ThreadHandle") in self.thread_handles:
                self.sequence = 1
        elif (call["api"] == "NtWriteVirtualMemory" or call["api"] == "NtWow64WriteVirtualMemory64" or call["api"] == "WriteProcessMemory" or call["api"] == "NtMapViewOfSection") and (self.sequence == 1 or self.sequence == 2):
            if self.get_argument(call, "ProcessHandle") in self.process_handles:
                self.sequence = self.sequence + 1
        elif (call["api"] == "NtSetContextThread") and (self.sequence == 1 or self.sequence == 2):
            if self.get_argument(call, "ThreadHandle") in self.thread_handles:
                self.sequence = self.sequence + 1
        elif call["api"] == "NtResumeThread" and (self.sequence == 2 or self.sequence == 3):
            handle = self.get_argument(call, "ThreadHandle")
            if handle in self.thread_handles:
                desc = "{0}({1}) -> {2}({3})".format(process["process_name"], str(process["process_id"]),
                                                     self.get_name_from_pid(self.thread_map[handle]), self.thread_map[handle])
                self.data.append({"Injection": desc})
                return True
        elif call["api"] == "NtResumeProcess" and (self.sequence == 2 or self.sequence == 3):
            handle = self.get_argument(call, "ProcessHandle")
            if handle in self.process_handles:
                desc = "{0}({1}) -> {2}({3})".format(process["process_name"], str(process["process_id"]),
                                                     self.get_name_from_pid(self.process_map[handle]), self.process_map[handle])
                self.data.append({"Injection": desc})
                return True
      
class CAPE_InjectionSetWindowLong(Signature):
    name = "InjectionSetWindowLong"
    description = "CAPE detection: Injection with SetWindowLong in a remote process"
    severity = 1
    categories = ["injection"]
    authors = ["kevoreilly"]
    minimum = "1.3"
    evented = True

    def __init__(self, *args, **kwargs):
        Signature.__init__(self, *args, **kwargs)
        self.lastprocess = None
        self.sharedsections = ["\\basenamedobjects\\shimsharedmemory",
                                "\\basenamedobjects\\windows_shell_global_counters",
                                "\\basenamedobjects\\msctf.shared.sfm.mih",
                                "\\basenamedobjects\\msctf.shared.sfm.amf",
                                "\\basenamedobjects\\urlzonessm_administrator",
                                "\\basenamedobjects\\urlzonessm_system"]

    filter_apinames = set(["NtMapViewOfSection", "NtOpenSection", "NtCreateSection", "FindWindowA", "FindWindowW", "FindWindowExA", "FindWindowExW", "PostMessageA", "PostMessageW", "SendNotifyMessageA", "SendNotifyMessageW", "SetWindowLongA", "SetWindowLongW", "SetWindowLongPtrA", "SetWindowLongPtrW"])

    def on_call(self, call, process):
        if process is not self.lastprocess:
            self.lastprocess = process
            self.window_handles = set()
            self.sharedmap = False
            self.windowfound = False

        if (call["api"] == ("NtMapViewOfSection")):
            handle = self.get_argument(call, "ProcessHandle")
            if handle != "0xffffffff":
                self.sharedmap = True
        elif call["api"] == "NtOpenSection" or call["api"] == "NtCreateSection":
            name = self.get_argument(call, "ObjectAttributes")
            if name.lower() in self.sharedsections:
                self.sharedmap = True
        elif call["api"].startswith("FindWindow") and call["status"] == True:
            self.windowfound = True
        elif call["api"].startswith("SetWindowLong") and call["status"] == True:
            if self.sharedmap == True and self.windowfound == True:
                return True
                
class CAPE_EvilGrab(Signature):
    name = "EvilGrab"
    description = "CAPE detection: EvilGrab"
    severity = 1
    categories = ["malware"]
    authors = ["kevoreilly"]
    minimum = "1.3"
    evented = True

    filter_apinames = set(["RegSetValueExA", "RegSetValueExW", "RegCreateKeyExA", "RegCreateKeyExW"])

    def __init__(self, *args, **kwargs):
        Signature.__init__(self, *args, **kwargs)
        self.reg_evilgrab_keyname = False
        self.reg_binary = False

    def on_call(self, call, process):
        if call["api"] == "RegCreateKeyExA" or call["api"] == "RegCreateKeyExW":
            buf = self.get_argument(call, "SubKey")
            if buf == "Software\\rar":
                self.reg_evilgrab_keyname = True
            
        if call["api"] == "RegSetValueExA" or call["api"] == "RegSetValueExW":
            length = self.get_raw_argument(call, "BufferLength")
            if length > 0x10000 and self.reg_evilgrab_keyname == True:
                self.reg_binary = True

    def on_complete(self):
        if self.reg_binary == True:
            return True
        else:
            return False

class CAPE_PlugX(Signature):
    name = "PlugX"
    description = "CAPE detection: PlugX"
    severity = 1
    categories = ["chinese", "malware"]
    families = ["plugx"]
    authors = ["kevoreilly"]
    minimum = "1.3"
    evented = True

    filter_apinames = set(["RtlDecompressBuffer", "memcpy"])

    def __init__(self, *args, **kwargs):
        Signature.__init__(self, *args, **kwargs)
        self.compressed_binary = False
        self.config_copy = False
        self.plugx = False

    def on_call(self, call, process):
        if call["api"] == "RtlDecompressBuffer":
            buf = self.get_raw_argument(call, "UncompressedBuffer")
            dos_header = buf[:64]
            if struct.unpack("<H", dos_header[0:2])[0] == IMAGE_DOS_SIGNATURE:
                self.compressed_binary = True
            elif struct.unpack("<H", dos_header[0:2])[0] == PLUGX_SIGNATURE:
                self.compressed_binary = True

        if call["api"] == "memcpy":
            count = self.get_raw_argument(call, "count")
            if (count == 0xae4)  or \
               (count == 0xbe4)  or \
               (count == 0x150c) or \
               (count == 0x1510) or \
               (count == 0x1516) or \
               (count == 0x170c) or \
               (count == 0x1b18) or \
               (count == 0x1d18) or \
               (count == 0x2540) or \
               (count == 0x254c) or \
               (count == 0x2d58) or \
               (count == 0x36a4) or \
               (count == 0x4ea4):
                self.config_copy = True

    def on_complete(self):
        if self.config_copy == True and self.compressed_binary == True:
            self.plugx = True
            return True

class CAPE_PlugX_fuzzy(Signature):
    name = "PlugX fuzzy"
    description = "CAPE detection: PlugX (fuzzy match)"
    severity = 1
    categories = ["chinese", "malware"]
    families = ["plugx"]
    authors = ["kevoreilly"]
    minimum = "1.3"
    evented = True

    filter_apinames = set(["RtlDecompressBuffer", "memcpy"])

    def __init__(self, *args, **kwargs):
        Signature.__init__(self, *args, **kwargs)
        self.compressed_binary = False
        self.config_copy = False
        self.plugx = False

    def on_call(self, call, process):
        if call["api"] == "RtlDecompressBuffer":
            buf = self.get_raw_argument(call, "UncompressedBuffer")
            dos_header = buf[:64]
            if struct.unpack("<H", dos_header[0:2])[0] == IMAGE_DOS_SIGNATURE:
                self.compressed_binary = True
            elif struct.unpack("<H", dos_header[0:2])[0] == PLUGX_SIGNATURE:
                self.plugx = True

    def on_complete(self):
        if self.config_copy == True and self.compressed_binary == True:
            self.plugx = True
        if self.plugx == True:
            return True
            