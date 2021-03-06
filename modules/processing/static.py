# Copyright (C) 2010-2015 Cuckoo Foundation, Optiv, Inc. (brad.spengler@optiv.com)
# This file is part of Cuckoo Sandbox - http://www.cuckoosandbox.org
# See the file 'docs/LICENSE' for copying permission.

import json
from lib.cuckoo.common.utils import store_temp_file
import lib.cuckoo.common.office.olefile as olefile
import lib.cuckoo.common.office.vbadeobf as vbadeobf
import lib.cuckoo.common.decoders.darkcomet as darkcomet
import lib.cuckoo.common.decoders.njrat as njrat
import lib.cuckoo.common.decoders.nanocore as nanocore
import lib.cuckoo.common.decoders.alienspy as alienspy
import lib.cuckoo.common.decoders.qrat as qrat
import logging
import os
import re
import math
import array
import base64
import hashlib

from datetime import datetime, timedelta
from lib.cuckoo.common.icon import PEGroupIconDir
from PIL import Image
from StringIO import StringIO
from datetime import datetime, date, time
from subprocess import Popen, PIPE
import struct

try:
    import bs4
    HAVE_BS4 = True
except ImportError:
    HAVE_BS4 = False

try:
    import magic
    HAVE_MAGIC = True
except ImportError:
    HAVE_MAGIC = False

try:
    import pefile
    import peutils
    HAVE_PEFILE = True
except ImportError:
    HAVE_PEFILE = False

try:
    import PyV8
    HAVE_PYV8 = True
except ImportError:
    HAVE_PYV8 = False

try:
    from M2Crypto import m2, BIO, X509, SMIME
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False

try:
    from whois import whois
    HAVE_WHOIS = True
except:
    HAVE_WHOIS = False

from lib.cuckoo.common.abstracts import Processing
from lib.cuckoo.common.constants import CUCKOO_ROOT
from lib.cuckoo.common.objects import File
from lib.cuckoo.common.office.oleid import OleID
from lib.cuckoo.common.office.olevba import detect_autoexec
from lib.cuckoo.common.office.olevba import detect_hex_strings
from lib.cuckoo.common.office.olevba import detect_patterns
from lib.cuckoo.common.office.olevba import detect_suspicious
from lib.cuckoo.common.office.olevba import filter_vba
from lib.cuckoo.common.office.olevba import VBA_Parser
from lib.cuckoo.common.utils import convert_to_printable
from lib.cuckoo.common.pdftools.pdfid import PDFiD, PDFiD2JSON
from lib.cuckoo.common.peepdf.PDFCore import PDFParser
from lib.cuckoo.common.peepdf.JSAnalysis import analyseJS

log = logging.getLogger(__name__)


# Obtained from
# https://github.com/erocarrera/pefile/blob/master/pefile.py
# Copyright Ero Carrera and released under the MIT License:
# https://github.com/erocarrera/pefile/blob/master/LICENSE

def _get_entropy(data):
    """ Computes the entropy value for the provided data
    @param data: data to be analyzed.
    @return: entropy value as float.
    """
    entropy = 0.0

    if len(data) == 0:
        return entropy

    occurrences = array.array('L', [0]*256)

    for x in data:
        occurrences[ord(x)] += 1

    for x in occurrences:
        if x:
            p_x = float(x) / len(data)
            entropy -= p_x*math.log(p_x, 2)

    return entropy

# Partially taken from
# http://malwarecookbook.googlecode.com/svn/trunk/3/8/pescanner.py

def _get_filetype(data):
    """Gets filetype, uses libmagic if available.
    @param data: data to be analyzed.
    @return: file type or None.
    """
    if not HAVE_MAGIC:
        return None

    try:
        ms = magic.open(magic.MAGIC_SYMLINK)
        ms.load()
        file_type = ms.buffer(data)
    except:
        try:
            file_type = magic.from_buffer(data)
        except Exception:
            return None
    finally:
        try:
            ms.close()
        except:
            pass

    return file_type

class DotNETExecutable(object):
    """.NET analysis"""

    def __init__(self, file_path, results):
        self.file_path = file_path
        self.results = results

    def add_statistic(self, name, field, value):
        self.results["statistics"]["processing"].append({
            "name": name,
            field: value,
        })

    def _get_custom_attrs(self):
        try:
            ret = []
            output = Popen(["/usr/bin/monodis", "--customattr", self.file_path], stdout=PIPE).stdout.read().split("\n")
            for line in output[1:]:
                splitline = line.split()
                if not splitline:
                    continue
                typeval = splitline[1].rstrip(":")
                nameval = splitline[6].split("::")[0]
                if "(string)" not in splitline[6]:
                    continue
                rem = " ".join(splitline[7:])
                startidx = rem.find("[\"")
                if startidx < 0:
                    continue
                endidx = rem.rfind("\"]")
                # also ignore empty strings
                if endidx <= 2:
                    continue
                valueval = rem[startidx+2:endidx-2]
                item = dict()
                item["type"] = convert_to_printable(typeval)
                item["name"] = convert_to_printable(nameval)
                item["value"] = convert_to_printable(valueval)
                ret.append(item)
            return ret
        except:
            return None

    def _get_assembly_refs(self):
        try:
            ret = []
            output = Popen(["/usr/bin/monodis", "--assemblyref", self.file_path], stdout=PIPE).stdout.read().split("\n")
            for idx in range(len(output)):
                splitline = output[idx].split("Version=")
                if len(splitline) < 2:
                    continue
                verval = splitline[1]
                splitline = output[idx+1].split("Name=")
                if len(splitline) < 2:
                    continue
                nameval = splitline[1]
                item = dict()
                item["name"] = convert_to_printable(nameval)
                item["version"] = convert_to_printable(verval)
                ret.append(item)
            return ret

        except:
            return None

    def _get_assembly_info(self):
        try:
            ret = dict()
            output = Popen(["/usr/bin/monodis", "--assembly", self.file_path], stdout=PIPE).stdout.read().split("\n")
            for line in output:
                if line.startswith("Name:"):
                    ret["name"] = convert_to_printable(line[5:].strip())
                if line.startswith("Version:"):
                    ret["version"] = convert_to_printable(line[8:].strip())
            return ret
        except:
            return None

    def _get_type_refs(self):
        try:
            ret = []
            output = Popen(["/usr/bin/monodis", "--typeref", self.file_path], stdout=PIPE).stdout.read().split("\n")
            for line in output[1:]:
                restline = ''.join(line.split(":")[1:])
                restsplit = restline.split("]")
                asmname = restsplit[0][2:]
                typename = ''.join(restsplit[1:])
                if asmname and typename:
                    item = dict()
                    item["assembly"] = convert_to_printable(asmname)
                    item["typename"] = convert_to_printable(typename)
                    ret.append(item)
            return sorted(ret)

        except:
            return None

    def run(self):
        """Run analysis.
        @return: analysis results dict or None.
        """
        if not os.path.exists(self.file_path):
            return None

        results = { }

        pretime = datetime.now()
        results["dotnet"] = { }
        results["dotnet"]["typerefs"] = self._get_type_refs()
        results["dotnet"]["assemblyrefs"] = self._get_assembly_refs()
        results["dotnet"]["assemblyinfo"] = self._get_assembly_info()
        results["dotnet"]["customattrs"] = self._get_custom_attrs()
        posttime = datetime.now()
        timediff = posttime - pretime
        self.add_statistic("static_dotnet", "time", float("%d.%03d" % (timediff.seconds, timediff.microseconds / 1000)))

        return results

class PortableExecutable(object):
    """PE analysis."""

    def __init__(self, file_path, results):
        """@param file_path: file path."""
        self.file_path = file_path
        self.pe = None
        self.results = results

    def add_statistic(self, name, field, value):
        self.results["statistics"]["processing"].append({
            "name": name,
            field: value,
        })

    def _get_peid_signatures(self):
        """Gets PEID signatures.
        @return: matched signatures or None.
        """
        if not self.pe:
            return None

        try:
            sig_path = os.path.join(CUCKOO_ROOT, "data",
                                    "peutils", "UserDB.TXT")
            signatures = peutils.SignatureDatabase(sig_path)
            return signatures.match_all(self.pe, ep_only=True)
        except:
            return None

    def _get_pdb_path(self):
        if not self.pe:
            return None

        try:
            for dbg in self.pe.DIRECTORY_ENTRY_DEBUG:
                dbgst = dbg.struct
                dbgdata = self.pe.__data__[dbgst.PointerToRawData:dbgst.PointerToRawData+dbgst.SizeOfData]
                if dbgst.Type == 4: #MISC
                    datatype, length, uniflag = struct.unpack_from("IIB", dbgdata)
                    return convert_to_printable(str(dbgdata[12:length]).rstrip('\0'))
                elif dbgst.Type == 2: #CODEVIEW
                    if dbgdata[:4] == "RSDS":
                        return convert_to_printable(str(dbgdata[24:]).rstrip('\0'))
                    elif dbgdata[:4] == "NB10":
                        return convert_to_printable(str(dbgdata[16:]).rstrip('\0'))
        except:
            pass

        return None

    def _get_imported_symbols(self):
        """Gets imported symbols.
        @return: imported symbols dict or None.
        """
        if not self.pe:
            return None

        imports = []

        if hasattr(self.pe, "DIRECTORY_ENTRY_IMPORT"):
            for entry in self.pe.DIRECTORY_ENTRY_IMPORT:
                try:
                    symbols = []
                    for imported_symbol in entry.imports:
                        symbol = {}
                        symbol["address"] = hex(imported_symbol.address)
                        symbol["name"] = imported_symbol.name
                        symbols.append(symbol)

                    imports_section = {}
                    imports_section["dll"] = convert_to_printable(entry.dll)
                    imports_section["imports"] = symbols
                    imports.append(imports_section)
                except:
                    continue

        return imports

    def _get_exported_dll_name(self):
        """Gets exported DLL name, if any
        @return: exported DLL name as string or None.
        """
        if not self.pe:
            return None

        if hasattr(self.pe, "DIRECTORY_ENTRY_EXPORT"):
            dllname = self.pe.get_string_at_rva(self.pe.DIRECTORY_ENTRY_EXPORT.struct.Name)
            # In recent versions of pefile, get_string_at_rva returns a Python3-style bytes object.
            # Convert it to a Python2-style string to ensure expected behavior when iterating
            # through it character by character.
            if type(dllname) is not str:
                dllname = "".join([chr(c) for c in dllname])

            return convert_to_printable(dllname)
        return None

    def _get_exported_symbols(self):
        """Gets exported symbols.
        @return: list of dicts of exported symbols or None.
        """
        if not self.pe:
            return None

        exports = []

        if hasattr(self.pe, "DIRECTORY_ENTRY_EXPORT"):
            for exported_symbol in self.pe.DIRECTORY_ENTRY_EXPORT.symbols:
                symbol = {}
                symbol["address"] = hex(self.pe.OPTIONAL_HEADER.ImageBase +
                                        exported_symbol.address)
                if exported_symbol.name:
                    symbol["name"] = convert_to_printable(exported_symbol.name)
                else:
                    symbol["name"] = ""
                symbol["ordinal"] = exported_symbol.ordinal
                exports.append(symbol)

        return exports

    def _get_directory_entries(self):
        """Gets image directory entries.
        @return: directory entries dict or None.
        """
        if not self.pe:
            return None

        dirents = []

        for entry in self.pe.OPTIONAL_HEADER.DATA_DIRECTORY:
            dirent = {}
            dirent["name"] = entry.name
            dirent["virtual_address"] = "0x{0:08x}".format(entry.VirtualAddress)
            dirent["size"] = "0x{0:08x}".format(entry.Size)
            dirents.append(dirent)

        return dirents

    def _convert_section_characteristics(self, val):
        flags = [ "", "", "", "IMAGE_SCN_TYPE_NO_PAD", "", "IMAGE_SCN_CNT_CODE", "IMAGE_SCN_CNT_INITIALIZED_DATA", "IMAGE_SCN_CNT_UNINITIALIZED_DATA", "IMAGE_SCN_LNK_OTHER",
                 "IMAGE_SCN_LNK_INFO", "", "IMAGE_SCN_LNK_REMOVE", "IMAGE_SCN_LNK_COMDAT", "", "IMAGE_SCN_NO_DEFER_SPEC_EXC", "IMAGE_SCN_GPREL", "", "IMAGE_SCN_MEM_PURGEABLE",
                 "IMAGE_SCN_MEM_LOCKED", "IMAGE_SCN_MEM_PRELOAD",
                 # alignment bytes
                 "", "", "", "",
                 "IMAGE_SCN_LNK_NRELOC_OVFL", "IMAGE_SCN_MEM_DISCARDABLE", "IMAGE_SCN_MEM_NOT_CACHED", "IMAGE_SCN_MEM_NOT_PAGED", "IMAGE_SCN_MEM_SHARED", "IMAGE_SCN_MEM_EXECUTE",
                 "IMAGE_SCN_MEM_READ", "IMAGE_SCN_MEM_WRITE"
                ]
        alignment = ["", "IMAGE_SCN_ALIGN_1BYTES", "IMAGE_SCN_ALIGN_2BYTES", "IMAGE_SCN_ALIGN_4BYTES", "IMAGE_SCN_ALIGN_8BYTES",
                     "IMAGE_SCN_ALIGN_16BYTES", "IMAGE_SCN_ALIGN_32BYTES", "IMAGE_SCN_ALIGN_64BYTES", "IMAGE_SCN_ALIGN_128BYTES", "IMAGE_SCN_ALIGN_256BYTES",
                     "IMAGE_SCN_ALIGN_512BYTES", "IMAGE_SCN_ALIGN_1024BYTES", "IMAGE_SCN_ALIGN_2048BYTES", "IMAGE_SCN_ALIGN_4096BYTES", "IMAGE_SCN_ALIGN_8192BYTES", ""
                    ]
        tags = []
        for idx, flagstr in enumerate(flags):
            if flags[idx] and (val & (1 << idx)):
                tags.append(flagstr)

        if val & 0x00F00000:
            alignval = (val >> 20) & 0xF
            if alignment[alignval]:
                tags.append(alignment[alignval])

        return "|".join(tags)

    def _get_sections(self):
        """Gets sections.
        @return: sections dict or None.
        """
        if not self.pe:
            return None

        sections = []

        for entry in self.pe.sections:
            try:
                section = {}
                section["name"] = convert_to_printable(entry.Name.strip("\x00"))
                section["raw_address"] = "0x{0:08x}".format(entry.PointerToRawData)
                section["virtual_address"] = "0x{0:08x}".format(entry.VirtualAddress)
                section["virtual_size"] = "0x{0:08x}".format(entry.Misc_VirtualSize)
                section["size_of_data"] = "0x{0:08x}".format(entry.SizeOfRawData)
                section["characteristics"] = self._convert_section_characteristics(entry.Characteristics)
                section["characteristics_raw"] = "0x{0:08x}".format(entry.Characteristics)
                section["entropy"] = "{0:.02f}".format(float(entry.get_entropy()))
                sections.append(section)
            except:
                continue

        return sections

    def _get_overlay(self):
        """Get information on the PE overlay
        @return: overlay dict or None.
        """
        if not self.pe:
            return None

        try:
            off = self.pe.get_overlay_data_start_offset()
        except:
            log.error("Your version of pefile is out of date.  Please update to the latest version on https://github.com/erocarrera/pefile")
            return None

        if off is None:
            return None
        overlay = {}
        overlay["offset"] = "0x{0:08x}".format(off)
        overlay["size"] = "0x{0:08x}".format(len(self.pe.__data__) - off)

        return overlay

    def _get_imagebase(self):
        """Get information on the Image Base
        @return: image base or None.
        """
        if not self.pe:
            return None

        return "0x{0:08x}".format(self.pe.OPTIONAL_HEADER.ImageBase)

    def _get_entrypoint(self):
        """Get full virtual address of entrypoint
        @return: entrypoint or None.
        """
        if not self.pe:
            return None

        return "0x{0:08x}".format(self.pe.OPTIONAL_HEADER.ImageBase + self.pe.OPTIONAL_HEADER.AddressOfEntryPoint)

    def _get_reported_checksum(self):
        """Get checksum from optional header
        @return: checksum or None.
        """
        if not self.pe:
            return None

        return "0x{0:08x}".format(self.pe.OPTIONAL_HEADER.CheckSum)

    def _get_actual_checksum(self):
        """Get calculated checksum of PE
        @return: checksum or None.
        """
        if not self.pe:
            return None

        retstr = None
        try:
            retstr = "0x{0:08x}".format(self.pe.generate_checksum())
        except:
            log.warning("Detected outdated version of pefile.  Please update to the latest version at https://github.com/erocarrera/pefile")
        return retstr

    def _get_osversion(self):
        """Get minimum required OS version for PE to execute
        @return: minimum OS version or None.
        """
        if not self.pe:
            return None

        return "{0}.{1}".format(self.pe.OPTIONAL_HEADER.MajorOperatingSystemVersion, self.pe.OPTIONAL_HEADER.MinorOperatingSystemVersion)

    def _get_resources(self):
        """Get resources.
        @return: resources dict or None.
        """
        if not self.pe:
            return None

        resources = []

        if hasattr(self.pe, "DIRECTORY_ENTRY_RESOURCE"):
            for resource_type in self.pe.DIRECTORY_ENTRY_RESOURCE.entries:
                try:
                    resource = {}

                    if resource_type.name is not None:
                        name = str(resource_type.name)
                    else:
                        name = str(pefile.RESOURCE_TYPE.get(resource_type.struct.Id))

                    if hasattr(resource_type, "directory"):
                        for resource_id in resource_type.directory.entries:
                            if hasattr(resource_id, "directory"):
                                for resource_lang in resource_id.directory.entries:
                                    data = self.pe.get_data(resource_lang.data.struct.OffsetToData, resource_lang.data.struct.Size)
                                    filetype = _get_filetype(data)
                                    language = pefile.LANG.get(resource_lang.data.lang, None)
                                    sublanguage = pefile.get_sublang_name_for_lang(resource_lang.data.lang, resource_lang.data.sublang)
                                    resource["name"] = name
                                    resource["offset"] = "0x{0:08x}".format(resource_lang.data.struct.OffsetToData)
                                    resource["size"] = "0x{0:08x}".format(resource_lang.data.struct.Size)
                                    resource["filetype"] = filetype
                                    resource["language"] = language
                                    resource["sublanguage"] = sublanguage
                                    resource["entropy"] = "{0:.02f}".format(float(_get_entropy(data)))
                                    resources.append(resource)
                except:
                    continue

        return resources

    def _get_icon_info(self):
        """Get icon in PNG format and information for searching for similar icons
        @return: tuple of (image data in PNG format encoded as base64, md5 hash of image data, md5 hash of "simplified" image for fuzzy matching)
        """
        if not self.pe:
            return None, None, None

        try:
            rt_group_icon_idx = [entry.id for entry in self.pe.DIRECTORY_ENTRY_RESOURCE.entries].index(pefile.RESOURCE_TYPE['RT_GROUP_ICON'])
            rt_group_icon_dir = self.pe.DIRECTORY_ENTRY_RESOURCE.entries[rt_group_icon_idx]
            entry = rt_group_icon_dir.directory.entries[0]
            offset = entry.directory.entries[0].data.struct.OffsetToData
            size = entry.directory.entries[0].data.struct.Size
            peicon = PEGroupIconDir(self.pe.get_memory_mapped_image()[offset:offset+size])
            bigwidth = 0
            bigheight = 0
            bigbpp = 0
            bigidx = -1
            iconidx = 0
            for idx,icon in enumerate(peicon.icons):
                if icon.bWidth >= bigwidth and icon.bHeight >= bigheight and icon.wBitCount >= bigbpp:
                    bigwidth = icon.bWidth
                    bigheight = icon.bHeight
                    bigbpp = icon.wBitCount
                    bigidx = icon.nID
                    iconidx = idx

            rt_icon_idx = [entry.id for entry in self.pe.DIRECTORY_ENTRY_RESOURCE.entries].index(pefile.RESOURCE_TYPE['RT_ICON'])
            rt_icon_dir = self.pe.DIRECTORY_ENTRY_RESOURCE.entries[rt_icon_idx]
            for entry in rt_icon_dir.directory.entries:
                if entry.id == bigidx:
                    offset = entry.directory.entries[0].data.struct.OffsetToData
                    size = entry.directory.entries[0].data.struct.Size
                    icon = peicon.get_icon_file(iconidx, self.pe.get_memory_mapped_image()[offset:offset+size])

                    strio = StringIO()
                    output = StringIO()

                    strio.write(icon)
                    strio.seek(0)
                    img = Image.open(strio)
                    img.save(output, format="PNG")

                    img = img.resize((8,8), Image.BILINEAR)
                    img = img.convert("RGB").convert("P", palette=Image.ADAPTIVE, colors=2).convert("L")
                    lowval = img.getextrema()[0]
                    img = img.point(lambda i: 255 if i > lowval else 0)
                    img = img.convert("1")
                    simplified = bytearray(img.getdata())

                    m = hashlib.md5()
                    m.update(output.getvalue())
                    fullhash = m.hexdigest()
                    m = hashlib.md5()
                    m.update(simplified)
                    simphash = m.hexdigest()
                    return base64.b64encode(output.getvalue()), fullhash, simphash
        except:
            pass

        return None, None, None

    def _get_versioninfo(self):
        """Get version info.
        @return: info dict or None.
        """
        if not self.pe:
            return None

        infos = []
        if hasattr(self.pe, "VS_VERSIONINFO"):
            if hasattr(self.pe, "FileInfo"):
                for entry in self.pe.FileInfo:
                    try:
                        if hasattr(entry, "StringTable"):
                            for st_entry in entry.StringTable:
                                for str_entry in st_entry.entries.items():
                                    entry = {}
                                    entry["name"] = convert_to_printable(str_entry[0])
                                    entry["value"] = convert_to_printable(str_entry[1])
                                    if entry["name"] == "Translation" and len(entry["value"]) == 10:
                                        entry["value"] = "0x0" + entry["value"][2:5] + " 0x0" + entry["value"][7:10]
                                    infos.append(entry)
                        elif hasattr(entry, "Var"):
                            for var_entry in entry.Var:
                                if hasattr(var_entry, "entry"):
                                    entry = {}
                                    entry["name"] = convert_to_printable(var_entry.entry.keys()[0])
                                    entry["value"] = convert_to_printable(var_entry.entry.values()[0])
                                    if entry["name"] == "Translation" and len(entry["value"]) == 10:
                                        entry["value"] = "0x0" + entry["value"][2:5] + " 0x0" + entry["value"][7:10]
                                    infos.append(entry)
                    except:
                        continue

        return infos


    def _get_imphash(self):
        """Gets imphash.
        @return: imphash string or None.
        """
        if not self.pe:
            return None

        try:
            return self.pe.get_imphash()
        except AttributeError:
            return None

    def _get_timestamp(self):
        """Get compilation timestamp.
        @return: timestamp or None.
        """
        if not self.pe:
            return None

        try:
            pe_timestamp = self.pe.FILE_HEADER.TimeDateStamp
        except AttributeError:
            return None

        return datetime.fromtimestamp(pe_timestamp).strftime("%Y-%m-%d %H:%M:%S")

    def _get_guest_digital_signers(self):
        retdata = dict()
        cert_data = dict()
        cert_info = os.path.join(CUCKOO_ROOT, "storage", "analyses",
                                 str(self.results["info"]["id"]), "aux",
                                 "DigiSig.json")

        if os.path.exists(cert_info):
            with open(cert_info, "r") as cert_file:
                buf = cert_file.read()
            if buf:
                cert_data = json.loads(buf)

        if cert_data:
            retdata = {
                "aux_sha1": cert_data["sha1"],
                "aux_timestamp": cert_data["timestamp"],
                "aux_valid": cert_data["valid"],
                "aux_error": cert_data["error"],
                "aux_error_desc": cert_data["error_desc"],
                "aux_signers": cert_data["signers"]
            }

        return retdata

    def _get_digital_signers(self):
        if not self.pe:
            return None

        retlist = None

        if HAVE_CRYPTO:
            address = self.pe.OPTIONAL_HEADER.DATA_DIRECTORY[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_SECURITY']].VirtualAddress

            #check if file is digitally signed
            if address == 0:
                return retlist

            signature = self.pe.write()[address+8:]

            # BIO.MemoryBuffer expects an argument of type 'str'
            if type(signature) is bytearray:
                signature = str(signature)

            bio = BIO.MemoryBuffer(signature)

            if bio:
                swig_pkcs7 = m2.pkcs7_read_bio_der(bio.bio_ptr())

                if swig_pkcs7:
                    p7 = SMIME.PKCS7(swig_pkcs7)
                    xst = p7.get0_signers(X509.X509_Stack())
                    retlist = []
                    if xst:
                        for cert in xst:
                            sn = cert.get_serial_number()
                            sha1_fingerprint = cert.get_fingerprint('sha1').lower().rjust(40, '0')
                            md5_fingerprint = cert.get_fingerprint('md5').lower().rjust(32, '0')
                            subject_str = str(cert.get_subject())
                            try:
                                cn = subject_str[subject_str.index("/CN=")+len("/CN="):]
                            except:
                                continue
                            retlist.append({
                                "sn": str(sn),
                                "cn": cn,
                                "sha1_fingerprint": sha1_fingerprint,
                                "md5_fingerprint": md5_fingerprint
                            })

        return retlist

    def run(self):
        """Run analysis.
        @return: analysis results dict or None.
        """
        if not os.path.exists(self.file_path):
            return {}

        try:
            self.pe = pefile.PE(self.file_path)
        except pefile.PEFormatError:
            return {}

        results = {}
        peresults = results["pe"] = { }

        pretime = datetime.now()
        peresults["peid_signatures"] = self._get_peid_signatures()
        posttime = datetime.now()
        timediff = posttime - pretime
        self.add_statistic("peid", "time", float("%d.%03d" % (timediff.seconds, timediff.microseconds / 1000)))

        peresults["imagebase"] = self._get_imagebase()
        peresults["entrypoint"] = self._get_entrypoint()
        peresults["reported_checksum"] = self._get_reported_checksum()
        peresults["actual_checksum"] = self._get_actual_checksum()
        peresults["osversion"] = self._get_osversion()
        peresults["pdbpath"] = self._get_pdb_path()
        peresults["imports"] = self._get_imported_symbols()
        peresults["exported_dll_name"] = self._get_exported_dll_name()
        peresults["exports"] = self._get_exported_symbols()
        peresults["dirents"] = self._get_directory_entries()
        peresults["sections"] = self._get_sections()
        peresults["overlay"] = self._get_overlay()
        peresults["resources"] = self._get_resources()
        peresults["icon"], peresults["icon_hash"], peresults["icon_fuzzy"] = self._get_icon_info()
        peresults["versioninfo"] = self._get_versioninfo()
        peresults["imphash"] = self._get_imphash()
        peresults["timestamp"] = self._get_timestamp()
        peresults["digital_signers"] = self._get_digital_signers()
        peresults["guest_signers"] = self._get_guest_digital_signers()
        peresults["imported_dll_count"] = len([x for x in peresults["imports"] if x.get("dll")])

        pretime = datetime.now()
        ratname = None
        ratconfig = None
        darkcomet_config = darkcomet.extract_config(self.file_path, self.pe)
        if darkcomet_config:
            ratname = "DarkComet"
            ratconfig = darkcomet_config
        njrat_config = njrat.extract_config(self.file_path)
        if njrat_config:
            ratname = "njRAT"
            ratconfig = njrat_config
        nanocore_config = nanocore.extract_config(self.pe)
        if nanocore_config:
            ratname = "NanoCore"
            ratconfig = nanocore_config
        posttime = datetime.now()
        timediff = posttime - pretime
        self.add_statistic("config_decoder", "time", float("%d.%03d" % (timediff.seconds, timediff.microseconds / 1000)))

        if ratname:
            results["rat"] = { }
            results["rat"]["name"] = ratname
            results["rat"]["config"] = ratconfig

        return results

class PDF(object):
    """PDF Analysis."""
    def __init__(self, file_path):
        self.file_path = file_path
        self.pdf = None
        self.base_uri = ""

    def _clean_string(self, value):
        # handle BOM for typical english unicode while avoiding some
        # invalid BOM seen in malicious PDFs (like using the utf16le BOM
        # for an ascii string)
        if value.startswith("\xfe\xff"):
            clean = True
            for x in value[2::2]:
                if ord(x):
                    clean = False
                    break
            if clean:
                return value[3::2]
        elif value.startswith("\xff\xfe"):
            clean = True
            for x in value[3::2]:
                if ord(x):
                    clean = False
                    break
            if clean:
                return value[2::2]
        return value

    def _get_obj_val(self, version, obj):
        try:
            if obj.type == "reference":
                return self.pdf.body[version].getObject(obj.id)
        except:
            pass
        return obj

    def _set_base_uri(self):
        try:
            for version in range(self.pdf.updates+1):
                trailer, streamTrailer = self.pdf.trailer[version]
                if trailer != None:
                    elem = trailer.dict.getElementByName("/Root")
                    elem = self._get_obj_val(version, elem)
                    elem = elem.getElementByName("/URI")
                    elem = self._get_obj_val(version, elem)
                    elem = elem.getElementByName("/Base")
                    elem = self._get_obj_val(version, elem)
                    self.base_uri = elem.getValue()
        except:
            pass

    def _parse(self, filepath):
        """Parses the PDF for static information. Uses PyV8 from peepdf to
        extract JavaScript from PDF objects.
        @param filepath: Path to file to be analyzed.
        @return: results dict or None.
        """
        # Load the PDF with PDFiD and convert it to JSON for processing
        pdf_data = PDFiD(filepath, False, True)
        pdf_json = PDFiD2JSON(pdf_data, True)
        pdfid_data = json.loads(pdf_json)[0]

        info = {}
        info["PDF Header"] = pdfid_data['pdfid']['header']
        info["Total Entropy"] = pdfid_data['pdfid']['totalEntropy']
        info['Entropy In Streams'] = pdfid_data['pdfid']['streamEntropy']
        info['Entropy Out Streams'] = pdfid_data['pdfid']['nonStreamEntropy']
        info['Count %% EOF'] = pdfid_data['pdfid']['countEof']
        info['Data After EOF'] = pdfid_data['pdfid']['countChatAfterLastEof']
        # Note, PDFiD doesn't interpret some dates properly, specifically it doesn't
        # seem to be able to properly represent time zones that involve fractions of
        # an hour
        dates = pdfid_data['pdfid']['dates']['date']

        # Get keywords, counts and format.
        keywords = {}
        for keyword in pdfid_data['pdfid']['keywords']['keyword']:
            keywords[str(keyword['name'])] = keyword['count']

        result = {}
        pdfresult = result["pdf"] = { }
        pdfresult["Info"] = info
        pdfresult["Dates"] = dates
        pdfresult["Keywords"] = keywords

        log.debug("About to parse with PDFParser")
        parser = PDFParser()
        ret, self.pdf = parser.parse(filepath, forceMode=True, looseMode=True, manualAnalysis=False)
        urlset = set()
        annoturiset = set()
        objects = []
        retobjects = []
        metadata = dict()

        self._set_base_uri()

        for i in range(len(self.pdf.body)):
            body = self.pdf.body[i]
            metatmp = self.pdf.getBasicMetadata(i)
            if metatmp:
                metadata = metatmp

            objects = body.objects

            for index in objects:
                oid = objects[index].id
                offset = objects[index].offset
                size = objects[index].size
                details = objects[index].object

                obj_data = {}
                obj_data["Object ID"] = oid
                obj_data["Offset"] = offset
                obj_data["Size"] = size
                if details.type == 'stream':
                    encoded_stream = details.encodedStream
                    decoded_stream = details.decodedStream
                    if HAVE_PYV8:
                        jsdata = None
                        try:
                            jslist, unescapedbytes, urlsfound, errors, ctxdummy = analyseJS(decoded_stream.strip())
                            jsdata = jslist[0]
                        except Exception,e:
                            continue
                        if len(errors):
                            continue
                        if jsdata == None:
                            continue

                        for url in urlsfound:
                            urlset.add(url)

                        # The following loop is required to "JSONify" the strings returned from PyV8.
                        # As PyV8 returns byte strings, we must parse out bytecode and
                        # replace it with an escape '\'. We can't use encode("string_escape")
                        # as this would mess up the new line representation which is used for
                        # beautifying the javascript code for Django's web interface.
                        ret_data = ""
                        for x in xrange(len(jsdata)):
                            if ord(jsdata[x]) > 127:
                                tmp = "\\x" + str(jsdata[x].encode("hex"))
                            else:
                                tmp = jsdata[x]
                            ret_data += tmp
                    else:
                        continue

                    obj_data["Data"] = ret_data
                    retobjects.append(obj_data)
                elif details.type == "dictionary" and details.hasElement("/A"):
                    # verify it to be a link type annotation
                    subtype_elem = details.getElementByName("/Subtype")
                    type_elem = details.getElementByName("/Type")
                    if not subtype_elem or not type_elem:
                        continue
                    subtype_elem = self._get_obj_val(i, subtype_elem)
                    type_elem = self._get_obj_val(i, type_elem)
                    if subtype_elem.getValue() != "/Link" or type_elem.getValue() != "/Annot":
                        continue
                    a_elem = details.getElementByName("/A")
                    a_elem = self._get_obj_val(i, a_elem)
                    if a_elem.type == "dictionary" and a_elem.hasElement("/URI"):
                        uri_elem = a_elem.getElementByName("/URI")
                        uri_elem = self._get_obj_val(i, uri_elem)
                        annoturiset.add(self.base_uri + uri_elem.getValue())
                else:
                    # can be dictionaries, arrays, etc, don't bother displaying them
                    # all for now
                    pass
                    #obj_data["File Type"] = "Encoded"
                    #obj_data["Data"] = "Encoded"
                    #retobjects.append(obj_data)

            pdfresult["JSStreams"] = retobjects

        if "creator" in metadata:
            pdfresult["Info"]["Creator"] = convert_to_printable(self._clean_string(metadata["creator"]))
        if "producer" in metadata:
            pdfresult["Info"]["Producer"] = convert_to_printable(self._clean_string(metadata["producer"]))
        if "author" in metadata:
            pdfresult["Info"]["Author"] = convert_to_printable(self._clean_string(metadata["author"]))

        if len(urlset):
            pdfresult["JS_URLs"] = list(urlset)
        if len(annoturiset):
            pdfresult["Annot_URLs"] = list(annoturiset)

        return result

    def run(self):
        """Run analysis.
        @return: analysis results dict or None.
        """
        if not os.path.exists(self.file_path):
            return None
        log.debug("Starting to load PDF")
        results = self._parse(self.file_path)
        return results

class Office(object):
    """Office Document Static Analysis"""
    def __init__(self, file_path):
        self.file_path = file_path

    # Parse a string-casted datetime object that olefile returns. This will parse
    # multiple types of timestamps including when a date is provide without a
    # time.
    def convert_dt_string(self, string):
        ctime = string.replace("datetime.datetime", "")
        ctime = ctime.replace("(","")
        ctime = ctime.replace(")","")
        ctime = "".join(ctime).split(", ")
        # Parse date, set to None if we don't have any/not enough data
        if len(ctime) >= 3:
            docdate = date(int(ctime[0]), int(ctime[1]), int(ctime[2])).strftime("%B %d, %Y")
        else:
            docdate = None
        # Parse if we are missing minutes and seconds field
        if len(ctime) == 4:
            doctime = time(int(ctime[3])).strftime("%H")
        # Parse if we are missing seconds field
        elif len(ctime) == 5:
            doctime = time(int(ctime[3]), int(ctime[4])).strftime("%H:%M")
        # Parse a full datetime string
        elif len(ctime) == 6:
            doctime = time(int(ctime[3]), int(ctime[4]), int(ctime[5])).strftime("%H:%M:%S")
        else:
            doctime = None

        if docdate and doctime:
            return docdate + " " + doctime
        elif docdate:
            return docdate
        else:
            return "None"

    def _parse(self, filepath):
        """Parses an office document for static information.
        Currently (as per olefile) the following formats are supported:
        - Word 97-2003 (.doc, .dot), Word 2007+ (.docm, .dotm)
        - Excel 97-2003 (.xls), Excel 2007+ (.xlsm, .xlsb)
        - PowerPoint 2007+ (.pptm, .ppsm)

        @param filepath: Path to the file to be analyzed.
        @return: results dict or None
        """

        results = dict()
        try:
            vba = VBA_Parser(filepath)
        except:
            return results

        officeresults = results["office"] = { }

        metares = officeresults["Metadata"] = dict()
        # The bulk of the metadata checks are in the OLE Structures
        # So don't check if we're dealing with XML.
        if olefile.isOleFile(filepath):
            ole = olefile.OleFileIO(filepath)
            meta = ole.get_metadata()
            # must be left this way or we won't see the results
            officeresults["Metadata"] = meta.get_meta()
            metares = officeresults["Metadata"]
            # Fix up some output formatting
            buf = self.convert_dt_string(metares["SummaryInformation"]["create_time"])
            metares["SummaryInformation"]["create_time"] = buf
            buf = self.convert_dt_string(metares["SummaryInformation"]["last_saved_time"])
            metares["SummaryInformation"]["last_saved_time"] = buf
            ole.close()
        if vba.detect_vba_macros():
            metares["HasMacros"] = "Yes"
            macrores = officeresults["Macro"] = dict()
            macrores["Code"] = dict()
            ctr = 0
            # Create IOC and category vars. We do this before processing the
            # macro(s) to avoid overwriting data when there are multiple
            # macros in a single file.
            macrores["Analysis"] = dict()
            macrores["Analysis"]["AutoExec"] = list()
            macrores["Analysis"]["Suspicious"] = list()
            macrores["Analysis"]["IOCs"] = list()
            macrores["Analysis"]["HexStrings"] = list()
            for (subfilename, stream_path, vba_filename, vba_code) in vba.extract_macros():
                vba_code = filter_vba(vba_code)
                if vba_code.strip() != '':
                    # Handle all macros
                    ctr += 1
                    outputname = "Macro" + str(ctr)
                    macrores["Code"][outputname] = list()
                    macrores["Code"][outputname].append((convert_to_printable(vba_filename),convert_to_printable(vba_code)))
                    autoexec = detect_autoexec(vba_code)
                    suspicious = detect_suspicious(vba_code)
                    iocs = vbadeobf.parse_macro(vba_code)
                    hex_strs = detect_hex_strings(vba_code)
                    if autoexec:
                        for keyword, description in autoexec:
                            macrores["Analysis"]["AutoExec"].append((keyword, description))
                    if suspicious:
                        for keyword, description in suspicious:
                            macrores["Analysis"]["Suspicious"].append((keyword, description))
                    if iocs:
                        for pattern, match in iocs:
                            macrores["Analysis"]["IOCs"].append((pattern, match))
                    if hex_strs:
                        for encoded, decoded in hex_strs:
                            macrores["Analysis"]["HexStrings"].append((encoded, decoded))
            # Delete and keys which had no results. Otherwise we pollute the
            # Django interface with null data.
            if macrores["Analysis"]["AutoExec"] == []:
                del macrores["Analysis"]["AutoExec"]
            if macrores["Analysis"]["Suspicious"] == []:
                del macrores["Analysis"]["Suspicious"]
            if macrores["Analysis"]["IOCs"] == []:
                del macrores["Analysis"]["IOCs"]
            if macrores["Analysis"]["HexStrings"] == []:
                del macrores["Analysis"]["HexStrings"]

        else:
            metares["HasMacros"] = "No"

        oleid = OleID(filepath)
        indicators = oleid.check()
        for indicator in indicators:
            if indicator.name == "Word Document" and indicator.value == True:
                metares["DocumentType"] = indicator.name
            if indicator.name == "Excel Workbook" and indicator.value == True:
                metares["DocumentType"] = indicator.name
            if indicator.name == "PowerPoint Presentation" and indicator.value == True:
                metares["DocumentType"] = indicator.name

        return results

    def run(self):
        """Run analysis.
        @return: analysis results dict or None.
        """
        if not os.path.exists(self.file_path):
            return None
        results = self._parse(self.file_path)
        return results

class Java(object):
    """Java Static Analysis"""
    def __init__(self, file_path, decomp_jar):
        self.file_path = file_path
        self.decomp_jar = decomp_jar

    def run(self):
        """Run analysis.
        @return: analysis results dict or None.
        """
        if not os.path.exists(self.file_path):
            return None

        results = {}

        results["java"] = { }
        
        if self.decomp_jar:
            f = open(self.file_path, "rb")
            data = f.read()
            f.close()
            jar_file = store_temp_file(data, "decompile.jar")

            try:
                p = Popen(["java", "-jar", self.decomp_jar, jar_file], stdout=PIPE)
                results["java"]["decompiled"] = convert_to_printable(p.stdout.read())
            except:
                pass

            try:
                os.unlink(jar_file)
            except:
                pass

        alienspy_config = alienspy.extract_config(self.file_path)
        if alienspy_config:
            results["rat"] = { }
            results["rat"]["name"] = "AlienSpy"
            results["rat"]["config"] = alienspy_config

        qrat_config = qrat.extract_config(self.file_path, self.decomp_jar)
        if qrat_config:
            results["rat"] = { }
            results["rat"]["name"] = "QRat"
            results["rat"]["config"] = qrat_config

        return results

class URL(object):
    """URL 'Static' Analysis"""
    def __init__(self, url):
        self.url = url
        p = r"^(?:https?:\/\/)?(?:www\.)?(?P<domain>[^:\/\n]+)"
        dcheck = re.match(p, self.url)
        if dcheck:
            self.domain = dcheck.group("domain")
            # Work around a bug where a "." can tail a url target if
            # someone accidentally appends one during submission
            while self.domain.endswith("."):
                self.domain = self.domain[:-1]
        else:
            self.domain = ""

    def run(self):
        results = {}
        if self.domain:
            try:
                w = whois(self.domain)
                results["url"] = {}
                # Create static fields if they don't exist, EG if the WHOIS
                # data is stale.
                fields = ['updated_date', 'status', 'name', 'city',
                          'expiration_date', 'zipcode', 'domain_name',
                          'country', 'whois_server', 'state', 'registrar',
                          'referral_url', 'address', 'name_servers', 'org',
                          'creation_date', 'emails']
                for field in fields:
                    if field not in w.keys() or not w[field]:
                        w[field] = ["None"]
            except:
                # No WHOIS data returned
                log.warning("No WHOIS data for domain: " + self.domain)
                return results

            # These can be a list or string, just make them all lists
            for key in w.keys():
                buf = list()
                # Handle and format dates
                if "_date" in key:
                    if isinstance(w[key], list):
                        buf = [str(dt).replace("T", " ").split(".")[0]
                                for dt in w[key]]
                    else:
                        buf = [str(w[key]).replace("T", " ").split(".")[0]]
                else:
                    if isinstance(w[key], list):
                        continue
                    else:
                        buf = [w[key]]
                w[key] = buf

            output = ("Name: {0}\nCountry: {1}\nState: {2}\nCity: {3}\n"
                      "ZIP Code: {4}\nAddress: {5}\n\nOrginization: {6}\n"
                      "Domain Name(s):\n    {7}\nCreation Date:\n    {8}\n"
                      "Updated Date:\n    {9}\nExpiration Date:\n    {10}\n"
                      "Email(s):\n    {11}\n\nRegistrar(s):\n    {12}\nName "
                      "Server(s):\n    {13}\nReferral URL(s):\n    {14}")
            output = output.format(w["name"][0], w["country"][0], w["state"][0],
                         w["city"][0], w["zipcode"][0], w["address"][0],
                         w["org"][0], "\n    ".join(w["domain_name"]),
                         "\n    ".join(w["creation_date"]),
                         "\n    ".join(w["updated_date"]),
                         "\n    ".join(w["expiration_date"]),
                         "\n    ".join(w["emails"]),
                         "\n    ".join(w["registrar"]),
                         "\n    ".join(w["name_servers"]),
                         "\n    ".join(w["referral_url"]))
            results["url"]["whois"] = output

        return results

class EncodedScriptFile(object):
    """Deobfuscates and interprets Windows Script Files."""
    encoding = [
        1, 2, 0, 1, 2, 0, 2, 0, 0, 2, 0, 2, 1, 0, 2, 0,
        1, 0, 2, 0, 1, 1, 2, 0, 0, 2, 1, 0, 2, 0, 0, 2,
        1, 1, 0, 2, 0, 2, 0, 1, 0, 1, 1, 2, 0, 1, 0, 2,
        1, 0, 2, 0, 1, 1, 2, 0, 0, 1, 1, 2, 0, 1, 0, 2,
    ]

    lookup = [
        [0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07,
         0x08, 0x7b, 0x0a, 0x0b, 0x0c, 0x0d, 0x0e, 0x0f,
         0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17,
         0x18, 0x19, 0x1a, 0x1b, 0x1c, 0x1d, 0x1e, 0x1f,
         0x32, 0x30, 0x21, 0x29, 0x5b, 0x38, 0x33, 0x3d,
         0x58, 0x3a, 0x35, 0x65, 0x39, 0x5c, 0x56, 0x73,
         0x66, 0x4e, 0x45, 0x6b, 0x62, 0x59, 0x78, 0x5e,
         0x7d, 0x4a, 0x6d, 0x71, 0x00, 0x60, 0x00, 0x53,
         0x00, 0x42, 0x27, 0x48, 0x72, 0x75, 0x31, 0x37,
         0x4d, 0x52, 0x22, 0x54, 0x6a, 0x47, 0x64, 0x2d,
         0x20, 0x7f, 0x2e, 0x4c, 0x5d, 0x7e, 0x6c, 0x6f,
         0x79, 0x74, 0x43, 0x26, 0x76, 0x25, 0x24, 0x2b,
         0x28, 0x23, 0x41, 0x34, 0x09, 0x2a, 0x44, 0x3f,
         0x77, 0x3b, 0x55, 0x69, 0x61, 0x63, 0x50, 0x67,
         0x51, 0x49, 0x4f, 0x46, 0x68, 0x7c, 0x36, 0x70,
         0x6e, 0x7a, 0x2f, 0x5f, 0x4b, 0x5a, 0x2c, 0x57],
        [0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07,
         0x08, 0x57, 0x0a, 0x0b, 0x0c, 0x0d, 0x0e, 0x0f,
         0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17,
         0x18, 0x19, 0x1a, 0x1b, 0x1c, 0x1d, 0x1e, 0x1f,
         0x2e, 0x47, 0x7a, 0x56, 0x42, 0x6a, 0x2f, 0x26,
         0x49, 0x41, 0x34, 0x32, 0x5b, 0x76, 0x72, 0x43,
         0x38, 0x39, 0x70, 0x45, 0x68, 0x71, 0x4f, 0x09,
         0x62, 0x44, 0x23, 0x75, 0x00, 0x7e, 0x00, 0x5e,
         0x00, 0x77, 0x4a, 0x61, 0x5d, 0x22, 0x4b, 0x6f,
         0x4e, 0x3b, 0x4c, 0x50, 0x67, 0x2a, 0x7d, 0x74,
         0x54, 0x2b, 0x2d, 0x2c, 0x30, 0x6e, 0x6b, 0x66,
         0x35, 0x25, 0x21, 0x64, 0x4d, 0x52, 0x63, 0x3f,
         0x7b, 0x78, 0x29, 0x28, 0x73, 0x59, 0x33, 0x7f,
         0x6d, 0x55, 0x53, 0x7c, 0x3a, 0x5f, 0x65, 0x46,
         0x58, 0x31, 0x69, 0x6c, 0x5a, 0x48, 0x27, 0x5c,
         0x3d, 0x24, 0x79, 0x37, 0x60, 0x51, 0x20, 0x36],
        [0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07,
         0x08, 0x6e, 0x0a, 0x0b, 0x0c, 0x0d, 0x0e, 0x0f,
         0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17,
         0x18, 0x19, 0x1a, 0x1b, 0x1c, 0x1d, 0x1e, 0x1f,
         0x2d, 0x75, 0x52, 0x60, 0x71, 0x5e, 0x49, 0x5c,
         0x62, 0x7d, 0x29, 0x36, 0x20, 0x7c, 0x7a, 0x7f,
         0x6b, 0x63, 0x33, 0x2b, 0x68, 0x51, 0x66, 0x76,
         0x31, 0x64, 0x54, 0x43, 0x00, 0x3a, 0x00, 0x7e,
         0x00, 0x45, 0x2c, 0x2a, 0x74, 0x27, 0x37, 0x44,
         0x79, 0x59, 0x2f, 0x6f, 0x26, 0x72, 0x6a, 0x39,
         0x7b, 0x3f, 0x38, 0x77, 0x67, 0x53, 0x47, 0x34,
         0x78, 0x5d, 0x30, 0x23, 0x5a, 0x5b, 0x6c, 0x48,
         0x55, 0x70, 0x69, 0x2e, 0x4c, 0x21, 0x24, 0x4e,
         0x50, 0x09, 0x56, 0x73, 0x35, 0x61, 0x4b, 0x58,
         0x3b, 0x57, 0x22, 0x6d, 0x4d, 0x25, 0x28, 0x46,
         0x4a, 0x32, 0x41, 0x3d, 0x5f, 0x4f, 0x42, 0x65],
    ]

    unescape = {
        "#": "\r", "&": "\n", "!": "<", "*": ">", "$": "@",
    }

    def __init__(self, filepath):
        self.filepath = filepath

    def run(self):
        results = {}
        source = open(self.filepath, "rb").read()
        source = self.decode(source)
        if not source:
            return results
        results["encscript"] = source[:65536]
        if len(source) > 65536:
            results["encscript"] += "\r\n<truncated>"
        return results

    def decode(self, source, start="#@~^", end="^#~@"):
        if start not in source or end not in source:
            return

        o = source.index(start) + len(start) + 8
        end = source.index(end) - 8

        c, m, r = 0, 0, []

        while o < end:
            ch = ord(source[o])
            if source[o] == "@":
                r.append(ord(self.unescape.get(source[o+1], "?")))
                c += r[-1]
                o, m = o + 1, m + 1
            elif ch < 128:
                r.append(self.lookup[self.encoding[m % 64]][ch])
                c += r[-1]
                m = m + 1
            else:
                r.append(ch)

            o = o + 1

        if (c % 2**32) != struct.unpack("I", source[o:o+8].decode("base64"))[0]:
            log.info("Invalid checksum for Encoded WSF file!")

        return "".join(chr(ch) for ch in r)

class WindowsScriptFile(object):
    script_re = "<\\s*script\\s*.*>.*?<\\s*/\\s*script\\s*>"

    def __init__(self, filepath):
        self.filepath = filepath

    def run(self):
        results = {}
        ret = []
        source = open(self.filepath, "rb").read()

        # Get rid of superfluous comments.
        source = re.sub("/\\*.*?\\*/", "", source, flags=re.S)

        for script in re.findall(self.script_re, source, re.I | re.S):
            try:
                x = bs4.BeautifulSoup(script, "html.parser")
                language = x.script.attrs.get("language", "").lower()
            except:
                language = None

            # We can't rely on bs4 or any other HTML/XML parser to provide us
            # with the raw content of the xml tag as they decode html entities
            # and all that, leaving us with a corrupted string.
            source = re.match("<.*>(.*)</.*>$", script, re.S).group(0)

            # Decode JScript.Encode encoding.
            if language in ("jscript.encode", "vbscript.encode"):
                source = EncodedScriptFile(filepath).decode(source)

            if (len(source) > 65536):
                source = source[:65536] + "\r\n<truncated>"

            ret.append(source)

        results["wsf"] = ret
        return results

class Static(Processing):
    """Static analysis."""

    def run(self):
        """Run analysis.
        @return: results dict.
        """
        self.key = "static"
        static = {}

        if self.task["category"] == "file":
            package = ""
            if "info" in self.results and "package" in self.results["info"]:
                package = self.results["info"]["package"]

            thetype = File(self.file_path).get_type()
            if HAVE_PEFILE and ("PE32" in thetype or "MS-DOS executable" in thetype):
                static = PortableExecutable(self.file_path, self.results).run()
                if static and "Mono" in thetype:
                    static.update(DotNETExecutable(self.file_path, self.results).run())
            elif "PDF" in thetype or self.task["target"].endswith(".pdf"):
                static = PDF(self.file_path).run()
            elif package in ("doc", "ppt", "xls"):
                static = Office(self.file_path).run()
            elif "Java Jar" in thetype or self.task["target"].endswith(".jar"):
                decomp_jar = self.options.get("procyon_path", None)
                if decomp_jar and not os.path.exists(decomp_jar):
                    log.error("procyon_path specified in processing.conf but the file does not exist.")
                static = Java(self.file_path, decomp_jar).run()
            # It's possible to fool libmagic into thinking our 2007+ file is a
            # zip. So until we have static analysis for zip files, we can use
            # oleid to fail us out silently, yeilding no static analysis
            # results for actual zip files.
            elif "Zip archive data, at least v2.0" in thetype:
                static = Office(self.file_path).run()
            elif package == "wsf" or thetype == "XML document text" or self.task["target"].endswith(".wsf") or package == "hta":
                static = WindowsScriptFile(self.file_path).run()
            elif package == "js" or package == "vbs":
                static = EncodedScriptFile(self.file_path).run()

        elif self.task["category"] == "url":
            enabled_whois = self.options.get("whois", True)
            if HAVE_WHOIS and enabled_whois:
                static = URL(self.task["target"]).run()

        return static
