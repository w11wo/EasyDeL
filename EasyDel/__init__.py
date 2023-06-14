__version__ = '0.0.0'

from .utils.checker import package_checker, is_jax_available, is_torch_available, is_flax_available, \
    is_tensorflow_available

if is_torch_available():
    ...
if is_jax_available():
    from .utils import make_shard_and_gather_fns
if is_tensorflow_available():
    ...
if is_flax_available():
    from .modules import FlaxLlamaModel, LlamaConfig, FlaxLlamaForCausalLM, LlamaModel, LlamaForCausalLM, \
        FlaxLTModelModule, FlaxLTConfig, FlaxLTForCausalLM, FlaxLTModel, GPTJConfig, FlaxGPTJModule, \
        FlaxGPTJForCausalLMModule, FlaxGPTJModel, FlaxGPTJForCausalLM, FlaxMptForCausalLM, MptConfig, FlaxMptModel

__all__ = __version__, 'package_checker', 'is_jax_available', 'is_torch_available', 'is_flax_available', \
    'is_tensorflow_available', 'LlamaConfig', 'LlamaForCausalLM', 'LlamaModel', 'FlaxLlamaForCausalLM', \
    'FlaxLlamaModel', 'FlaxGPTJModule', 'FlaxGPTJForCausalLMModule', \
    'FlaxGPTJModel', 'FlaxGPTJForCausalLM', 'GPTJConfig', \
    'FlaxLTModel', 'FlaxLTConfig', 'FlaxLTModelModule', 'FlaxLTForCausalLM', \
    "FlaxMptForCausalLM", "MptConfig", "FlaxMptModel"
