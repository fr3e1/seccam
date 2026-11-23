// Proto 2 viewer frame: "SC" 0x02 0x01 <uint32 BE header length> <JSON header> <JPEG bytes>.
const MAGIC_0 = 0x53;
const MAGIC_1 = 0x43;

export function decodeViewerFrame(buf) {
  const bytes = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
  if (bytes.length < 8) throw new Error('frame too short');
  if (bytes[0] !== MAGIC_0 || bytes[1] !== MAGIC_1) throw new Error('bad magic');
  if (bytes[2] !== 2) throw new Error('unsupported version');
  if (bytes[3] !== 1) throw new Error('unsupported frame type');
  const hlen = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength).getUint32(4, false);
  if (hlen > bytes.length - 8) throw new Error('header length overrun');
  let header;
  try {
    header = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes.subarray(8, 8 + hlen)));
  } catch {
    throw new Error('bad header');
  }
  if (header === null || typeof header !== 'object' || Array.isArray(header)) throw new Error('bad header');
  return { header, jpeg: bytes.subarray(8 + hlen) };
}
