# Load and dump a diagnostics database in CDD format.
import logging

from xml.etree import ElementTree

from ..data import Data
from ..did import Did
from ..internal_database import InternalDatabase
from ...errors import Error


LOGGER = logging.getLogger(__name__)

def saw_tooth_from_linear_bitnum(linear_bit_num):
    '''Convert from linear bit offset to the saw-tooth
    bit numbering scheme that is assumed by the
    bitstream encoder/decoder.

    Byte Num         |    0    |    1    |
                     |MSb   LSb|MSb   LSb|
    Linear Bit Num   |0  ...  7|8  ... 15| << input
    Sawtooth Bit Num |7  ...  0|15 ...  8| << output
    '''
    byte_num = linear_bit_num // 8
    linear_bit_offset = linear_bit_num % 8
    saw_bit_offset = 7 - linear_bit_offset
    return (byte_num * 8) + saw_bit_offset

def saw_tooth_start_from_linear_bitnum(byte_order, linear_first_bit, bit_len):
    '''Convert from sequential first field bit numbering, as used in
    CDD files, to the saw-tooth field start-bit numbering scheme
    that is assumed by the bitstream encoder/decoder.

    There are two aspects to this conversion:

    1. The start bit identification convention

    BigEndian fields start at their MSBit
    LittleEndian fields start at their LSBit

    2. The saw tooth bit numbering convention
    '''
    # Start bit convention (in linear space as it is easy)
    if byte_order == 'big_endian':
        start_bit = linear_first_bit  # MSBit position
    elif byte_order == 'little_endian':
        start_bit = linear_first_bit + bit_len - 1  # LSBit position
    else:
        raise Error("Unknown byte order: %s" % byte_order)

    # Convert to Saw tooth bit num space
    return saw_tooth_from_linear_bitnum(start_bit)



class DataType(object):

    def __init__(self,
                 name,
                 id_,
                 bit_length,
                 encoding,
                 minimum,
                 maximum,
                 choices,
                 byte_order,
                 unit,
                 factor,
                 offset):
        self.name = name
        self.id_ = id_
        self.bit_length = bit_length
        self.encoding = encoding
        self.minimum = minimum
        self.maximum = maximum
        self.choices = choices
        self.byte_order = byte_order
        self.unit = unit
        self.factor = factor
        self.offset = offset


def _load_choices(data_type):
    choices = {}

    for choice in data_type.findall('TEXTMAP'):
        start = int(choice.attrib['s'].strip('()'))
        end = int(choice.attrib['e'].strip('()'))

        if start == end:
            choices[start] = choice.find('TEXT/TUV[1]').text

    if not choices:
        choices = None

    return choices


def _load_data_types(ecu_doc):
    """Load all data types found in given ECU doc element.

    """

    data_types = {}

    types = ecu_doc.findall('DATATYPES/IDENT')
    types += ecu_doc.findall('DATATYPES/LINCOMP')
    types += ecu_doc.findall('DATATYPES/TEXTTBL')
    types += ecu_doc.findall('DATATYPES/STRUCTDT')
    types += ecu_doc.findall('DATATYPES/EOSITERDT')

    for data_type in types:
        # Default values.
        byte_order = 'big_endian'
        unit = None
        factor = 1
        offset = 0
        bit_length = None
        encoding = None
        minimum = None
        maximum = None

        # Name and id.
        type_name = data_type.find('NAME/TUV[1]').text
        type_id = data_type.attrib['id']

        # Load from C-type element.
        ctype = data_type.find('CVALUETYPE')

        for key, value in ctype.attrib.items():
            if key == 'bl':
                bit_length = int(value)
            elif key == 'enc':
                encoding = value
            elif key == 'minsz':
                minimum = int(value)
            elif key == 'maxsz':
                maximum = int(value)
            else:
                LOGGER.debug("Ignoring unsupported attribute '%s'.", key)

        # Decode byte order
        bo_code = ctype.attrib['bo']
        if bo_code == '21':
            byte_order = 'big_endian'
        elif bo_code == '12':
            byte_order = 'little_endian'
        else:
            raise Error("Unsupported byte order code '%s'.", bo_code)

        # Load from P-type element.
        ptype_unit = data_type.find('PVALUETYPE/UNIT')

        if ptype_unit is not None:
            unit = ptype_unit.text

        # Choices, scale and offset.
        choices = _load_choices(data_type)

        # Slope and offset.
        comp = data_type.find('COMP')

        if comp is not None:
            factor = float(comp.attrib['f'])
            offset = float(comp.attrib['o'])

        data_types[type_id] = DataType(type_name,
                                       type_id,
                                       bit_length,
                                       encoding,
                                       minimum,
                                       maximum,
                                       choices,
                                       byte_order,
                                       unit,
                                       factor,
                                       offset)

    return data_types


def _load_data_element(data, offset, data_types):
    """Load given signal element and return a signal object.

    """

    data_type = data_types[data.attrib['dtref']]

    start_bit_index = saw_tooth_start_from_linear_bitnum(data_type.byte_order, offset, data_type.bit_length)

    return Data(name=data.find('QUAL').text,
                start = start_bit_index,
                length=data_type.bit_length,
                byte_order = data_type.byte_order,
                scale=data_type.factor,
                offset=data_type.offset,
                minimum=data_type.minimum,
                maximum=data_type.maximum,
                unit=data_type.unit,
                choices=data_type.choices)


def _load_did_element(did, data_types):
    """Load given DID element and return a did object.

    """

    offset = 0
    datas = []
    data_objs = did.findall('SIMPLECOMPCONT/DATAOBJ')
    data_objs += did.findall('SIMPLECOMPCONT/UNION/STRUCT/DATAOBJ')

    for data_obj in data_objs:
        data = _load_data_element(data_obj,
                                  offset,
                                  data_types)

        if data:
            datas.append(data)
            offset += data.length

    identifier = int(did.find('STATICVALUE').attrib['v'])
    name = did.find('QUAL').text
    length = (offset + 7) // 8

    return Did(identifier=identifier,
               name=name,
               length=length,
               datas=datas)


def load_string(string):
    """Parse given CDD format string.

    """

    root = ElementTree.fromstring(string)
    ecu_doc = root.find('ECUDOC')
    data_types = _load_data_types(ecu_doc)
    var = ecu_doc.findall('ECU')[0].find('VAR')
    dids = []

    for diag_class in var.findall('DIAGCLASS'):
        for diag_inst in diag_class.findall('DIAGINST'):
            did = _load_did_element(diag_inst,
                                    data_types)
            dids.append(did)

    return InternalDatabase(dids)
