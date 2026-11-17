// Hand-written types for the generated Emscripten module `pdq-wasm.js`
// (built by frontend/wasm/pdq/build.sh; the .js itself is generated and not typechecked).
//
// Only the exports build.sh asks for are declared here.

export interface PdqWasmModule {
  /** Packed RGB24 input. Returns the PDQ quality (0..100), or -1 for bad dimensions. */
  _marceline_pdq_dihedral_rgb(
    rgbPtr: number,
    numRows: number,
    numCols: number,
    outPtr: number,
  ): number;
  /** Single-plane 8-bit luma input. Returns the PDQ quality (0..100), or -1. */
  _marceline_pdq_dihedral_luma(
    greyPtr: number,
    numRows: number,
    numCols: number,
    outPtr: number,
  ): number;
  _marceline_pdq_abi_version(): number;
  _malloc(size: number): number;
  _free(ptr: number): void;
  /** Re-read after every allocation: memory growth detaches the old view. */
  readonly HEAPU8: Uint8Array;
}

declare const createPdqModule: (options?: Record<string, unknown>) => Promise<PdqWasmModule>;

export default createPdqModule;
