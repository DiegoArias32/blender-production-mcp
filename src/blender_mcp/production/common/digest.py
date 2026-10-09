"""Equality fingerprints shared by the host and Blender payloads.

Safe mode denies ``hashlib`` inside Blender payloads. M1-M3 used a pure-Python SHA-256
(``m2_sha256``), measured at 0.33 MB/s inside Blender 5.2.1 (audit AUD-009): a 100 000-key snapshot
(~20-28 MB of canonical JSON) needed roughly a minute per digest.

M3.1 fingerprints runtime state with ``m2_fast_digest``: the canonical JSON bytes read as one big
integer, reduced modulo two Mersenne primes (2^521-1 and 2^607-1), plus the byte length. CPython
performs the reduction in C, the definition is plain integer arithmetic (identical on the host and
in Blender's Python), and any localized change of fewer than 65 bytes changes the result with
certainty (the prime cannot divide the difference). It detects accidental change; it is **not** a
cryptographic hash, a MAC or an authentication mechanism. Host-only integrity (proposal files) still
uses hashlib SHA-256.
"""
import inspect
import json


def m2_sha256(payload):
    constants = [
        0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
        0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
        0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
        0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
        0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
        0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
        0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
        0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2]
    state=[0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19]
    data=bytearray(payload)
    size=len(data)*8
    data.append(128)
    data.extend([0]*((56-len(data)%64)%64))
    data.extend(size.to_bytes(8,'big'))
    mask=0xffffffff
    def rotate(value,count):
        return ((value >> count) | (value << (32-count))) & mask
    for start in range(0,len(data),64):
        words=[int.from_bytes(data[index:index+4],'big') for index in range(start,start+64,4)]
        for index in range(16,64):
            first=rotate(words[index-15],7)^rotate(words[index-15],18)^(words[index-15]>>3)
            second=rotate(words[index-2],17)^rotate(words[index-2],19)^(words[index-2]>>10)
            words.append((words[index-16]+first+words[index-7]+second)&mask)
        a,b,c,d,e,f,g,h=state
        for index in range(64):
            upper=rotate(e,6)^rotate(e,11)^rotate(e,25)
            choose=(e&f)^((~e)&g)
            first=(h+upper+choose+constants[index]+words[index])&mask
            lower=rotate(a,2)^rotate(a,13)^rotate(a,22)
            majority=(a&b)^(a&c)^(b&c)
            second=(lower+majority)&mask
            h,g,f,e,d,c,b,a=g,f,e,(d+first)&mask,c,b,a,(first+second)&mask
        state=[(one+two)&mask for one,two in zip(state,[a,b,c,d,e,f,g,h])]
    return ''.join(format(value,'08x') for value in state)


def m2_fast_digest(payload):
    number=int.from_bytes(payload,'big')
    return 'm521:'+format(len(payload),'x')+':'+format(number%((1<<521)-1),'x')+':'+format(number%((1<<607)-1),'x')


def m2_digest(value):
    return m2_fast_digest(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8'))


def runtime_digest(value):
    """Host-side twin of the Blender ``m2_digest``: fingerprints compared across the socket."""
    return m2_digest(value)


DIGEST_RUNTIME=inspect.getsource(m2_sha256)+'\n'+inspect.getsource(m2_fast_digest)+'\n'+inspect.getsource(m2_digest)
