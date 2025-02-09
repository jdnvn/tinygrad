from __future__ import annotations
import os, subprocess, pathlib, ctypes, tempfile, functools
import Metal, libdispatch
from typing import List, Any, Tuple
from tinygrad.codegen.kernel import LinearizerOptions
from tinygrad.helpers import prod, getenv, DEBUG, DType, diskcache, unwrap2
from tinygrad.device import Compiled, LRUAllocator
from tinygrad.renderer.metal import MetalRenderer

@diskcache
def compile_metal(prg, use_xcode=bool(getenv("METAL_XCODE"))) -> bytes:
  assert MetalDevice.compiler_device, "metal device creation is required for metal compile"
  if use_xcode:
    # NOTE: if you run llvm-dis on "air" you can see the llvm bytecode
    air = subprocess.check_output(['xcrun', '-sdk', 'macosx', 'metal', '-x', 'metal', '-c', '-', '-o', '-'], input=prg.encode('utf-8'))
    return subprocess.check_output(['xcrun', '-sdk', 'macosx', 'metallib', '-', '-o', '-'], input=air)
  options = Metal.MTLCompileOptions.new()
  library = unwrap2(MetalDevice.compiler_device.newLibraryWithSource_options_error_(prg, options, None))
  return library.libraryDataContents().bytes().tobytes()

class MetalProgram:
  def __init__(self, device:MetalDevice, name:str, lib:bytes, bufs:int, vars:int=0):
    self.device = device
    data = libdispatch.dispatch_data_create(lib, len(lib), None, None)
    self.library = unwrap2(self.device.device.newLibraryWithData_error_(data, None))
    self.fxn = self.library.newFunctionWithName_(name)
    if DEBUG >= 6:
      with tempfile.NamedTemporaryFile(delete=True) as shader:
        shader.write(lib)
        shader.flush()
        os.system(f"cd {pathlib.Path(__file__).parents[2]}/disassemblers/applegpu && python3 compiler_explorer.py {shader.name}")
    self.pipeline_state = unwrap2(self.device.device.newComputePipelineStateWithFunction_error_(self.fxn, None))

  def __call__(self, *bufs, global_size:Tuple[int,int,int], local_size:Tuple[int,int,int], wait=False):
    assert prod(local_size) <= self.pipeline_state.maxTotalThreadsPerThreadgroup(), f"local size {local_size} bigger than {self.pipeline_state.maxTotalThreadsPerThreadgroup()} with exec width {self.pipeline_state.threadExecutionWidth()} memory length {self.pipeline_state.staticThreadgroupMemoryLength()}"
    command_buffer = self.device.mtl_queue.commandBuffer()
    encoder = command_buffer.computeCommandEncoder()
    encoder.setComputePipelineState_(self.pipeline_state)
    for i,a in enumerate(bufs):
      if isinstance(a, int): encoder.setBytes_length_atIndex_((arg:=ctypes.c_int32(a)), ctypes.sizeof(arg), i)
      else: encoder.setBuffer_offset_atIndex_(a, 0, i)
    encoder.dispatchThreadgroups_threadsPerThreadgroup_(Metal.MTLSize(*global_size), Metal.MTLSize(*local_size))
    encoder.endEncoding()
    command_buffer.commit()
    if wait:
      command_buffer.waitUntilCompleted()
      return command_buffer.GPUEndTime() - command_buffer.GPUStartTime()
    self.device.mtl_buffers_in_flight.append(command_buffer)

class MetalAllocator(LRUAllocator):
  def __init__(self, device:MetalDevice):
    self.device:MetalDevice = device
    super().__init__()
  def _alloc(self, size:int, dtype:DType):
    if size == 0: return None
    ret = self.device.device.newBufferWithLength_options_(size*dtype.itemsize, Metal.MTLResourceStorageModeShared)
    if ret is None: raise MemoryError(f"Metal OOM while allocating {size=} {dtype=}")
    return ret
  def _async_copy(self, dest, src):
    assert src.length() == dest.length(), f"length mismatch {src.length()=} {dest.length()=}"
    command_buffer = self.device.mtl_queue.commandBuffer()
    encoder = command_buffer.blitCommandEncoder()
    encoder.copyFromBuffer_sourceOffset_toBuffer_destinationOffset_size_(src, 0, dest, 0, src.length())
    encoder.endEncoding()
    command_buffer.commit()
    self.device.mtl_buffers_in_flight.append(command_buffer)
  def _from_buffer(self, src:memoryview): return self.device.device.newBufferWithBytesNoCopy_length_options_deallocator_(src, len(src), Metal.MTLResourceStorageModeShared, None)
  def _free(self, opaque): opaque.release()
  def as_buffer(self, src) -> memoryview:
    self.device.synchronize()
    return src.contents().as_buffer(src.length())
  def copyin(self, dest, src:memoryview):
    src_from_buffer = None if getenv("SLOW_METAL_COPY") else self._from_buffer(src)
    if src_from_buffer is None:
      self.as_buffer(dest)[:] = src
    else:
      self.device.copies_in_flight.append(src)
      self._async_copy(dest, src_from_buffer)
  def copyout(self, dest:memoryview, src): dest[:] = self.as_buffer(src)

class MetalDevice(Compiled):
  compiler_device = None
  def __init__(self, device:str):
    self.device = Metal.MTLCreateSystemDefaultDevice()
    if MetalDevice.compiler_device is None: MetalDevice.compiler_device = self.device
    self.mtl_queue = self.device.newCommandQueueWithMaxCommandBufferCount_(1024)
    self.mtl_buffers_in_flight: List[Any] = []
    self.copies_in_flight: List[memoryview] = []
    from tinygrad.runtime.graph.metal import MetalGraph
    super().__init__(MetalAllocator(self), LinearizerOptions(device="METAL"), MetalRenderer, compile_metal, functools.partial(MetalProgram, self), functools.partial(MetalGraph, self))
  def synchronize(self):
    for cbuf in self.mtl_buffers_in_flight: cbuf.waitUntilCompleted()
    self.copies_in_flight.clear()
    self.mtl_buffers_in_flight.clear()
