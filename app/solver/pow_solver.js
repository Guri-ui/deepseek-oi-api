const fs = require('fs');
const path = require('path');

const wasmBuffer = fs.readFileSync(path.join(__dirname, 'sha3_wasm.wasm'));
let wasmInstance = null;

async function initWasm() {
  if (!wasmInstance) {
    const wasmModule = await WebAssembly.instantiate(wasmBuffer, { wbg: {} });
    wasmInstance = wasmModule.instance.exports;
  }
  return wasmInstance;
}

let WASM_VECTOR_LEN = 0;
const encoder = new TextEncoder();

function passStringToWasm(wasm, arg) {
  const buf = encoder.encode(arg);
  const ptr = wasm.__wbindgen_export_0(buf.length, 1) >>> 0;
  new Uint8Array(wasm.memory.buffer).subarray(ptr, ptr + buf.length).set(buf);
  WASM_VECTOR_LEN = buf.length;
  return ptr;
}

async function solve(algorithm, challenge, salt, difficulty, expireAt, signature, targetPath) {
  if (algorithm !== 'DeepSeekHashV1') {
    throw new Error('Unsupported algorithm: ' + algorithm);
  }
  const wasm = await initWasm();
  const prefix = `${salt}_${expireAt}_`;
  const retptr = wasm.__wbindgen_add_to_stack_pointer(-16);
  try {
    const ptr0 = passStringToWasm(wasm, challenge);
    const len0 = WASM_VECTOR_LEN;
    const ptr1 = passStringToWasm(wasm, prefix);
    const len1 = WASM_VECTOR_LEN;

    wasm.wasm_solve(retptr, ptr0, len0, ptr1, len1, difficulty);
    const dataView = new DataView(wasm.memory.buffer);
    const r0 = dataView.getInt32(retptr + 0, true);
    const r1 = dataView.getFloat64(retptr + 8, true);
    if (r0 === 0) return null;
    return r1;
  } finally {
    wasm.__wbindgen_add_to_stack_pointer(16);
  }
}

if (require.main === module) {
  const args = process.argv.slice(2);
  if (args.length >= 6) {
    const [algorithm, challenge, salt, difficulty, expireAt, signature, targetPath] = args;
    solve(algorithm, challenge, salt, parseInt(difficulty), parseInt(expireAt), signature, targetPath || '/api/v0/chat/completion')
      .then(answer => {
        const responseObj = {
          algorithm,
          challenge,
          salt,
          answer,
          signature,
          target_path: targetPath || '/api/v0/chat/completion'
        };
        const header = Buffer.from(JSON.stringify(responseObj)).toString('base64');
        console.log(JSON.stringify({ answer, header }));
      })
      .catch(err => {
        console.error(err);
        process.exit(1);
      });
  } else {
    console.error("Usage: node pow_solver.js <algorithm> <challenge> <salt> <difficulty> <expireAt> <signature> [targetPath]");
    process.exit(1);
  }
}

module.exports = { solve };
