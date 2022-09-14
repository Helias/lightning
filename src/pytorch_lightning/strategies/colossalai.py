from typing import Any, Dict, List, Optional

import torch
from lightning_utilities.core.imports import RequirementCache

import pytorch_lightning as pl
from lightning_lite.plugins.environments.cluster_environment import ClusterEnvironment
from pytorch_lightning.accelerators.cuda import CUDAAccelerator
from pytorch_lightning.overrides.base import _LightningModuleWrapperBase, _LightningPrecisionModuleWrapperBase
from pytorch_lightning.plugins.io.checkpoint_plugin import CheckpointIO
from pytorch_lightning.plugins.precision import ColossalAIPrecisionPlugin, PrecisionPlugin
from pytorch_lightning.strategies.ddp import DDPStrategy
from pytorch_lightning.utilities.exceptions import MisconfigurationException
from pytorch_lightning.utilities.types import STEP_OUTPUT

_COLOSSALAI_AVAILABLE = RequirementCache("colossalai")
if _COLOSSALAI_AVAILABLE:
    from colossalai.context import ParallelMode
    from colossalai.core import global_context as gpc
    from colossalai.gemini import ChunkManager, GeminiManager
    from colossalai.logging import disable_existing_loggers, get_dist_logger
    from colossalai.nn.optimizer import CPUAdam, HybridAdam
    from colossalai.nn.parallel import ZeroDDP
    from colossalai.tensor import ProcessGroup
    from colossalai.utils import get_current_device
    from colossalai.utils.model.colo_init_context import ColoInitContext
    from colossalai.zero import ZeroOptimizer


class ColossalAIStrategy(DDPStrategy):
    """ColossalAI strategy. It only supports a single optimizer, which must be
    :class:`colossalai.nn.optimizer.CPUAdam` or :class:`colossalai.nn.optimizer.HybridAdam` now. You must
    initialize your model in ``LightningModule.configure_sharded_model()``.

    It configures accelerator and precision, and you should not configure them when initializing ``Trainer``.
    CUDA is essential for this strategy. Please make sure CUDA is available.

    Example::

        class GLUETransformer(LightningModule):
            ...
            def configure_sharded_model(self) -> None:
                self.model = BertForSequenceClassification.from_pretrained('bert-base-uncased')
            def on_load_checkpoint(self, checkpoint) -> None:
                if not hasattr(self, 'model'):
                    self.configure_sharded_model()
        trainer = Trainer(..., accelerator="gpu", precision=16, strategy="colossalai")

    Args:
        use_chunk: Whether to use chunk-based memory management.
            It can speed up training, but slightly more memory will be used.

        chunk_size: The size of a chunk.
            It will be ignored when ``use_chunk=False``.
            If it's None, a best chunk size will be searched out based on ``chunk_search_range``,
            ``chunk_search_n_grids`` and ``min_chunk_size``.

        enable_distributed_storage: Whether to storage model in a distributed manner.
            It reduces memory from 1 to 1/N, but it may slow down training.

        placement_policy: It can be "cpu", "cuda" and "auto".

            * If it's "cpu", parameters, gradients and optimizer states will be offloaded to CPU,
                which means min CUDA memory will be used.
            * If it's "cuda", they won't be offloaded, which means max CUDA memory will be used. It's the fastest.
            * If it's "auto", they are moving dynamically based on CPU and CUDA memory usage.
                It will utilize heterogeneous memory space evenly and well.
                Note that "auto" policy can only work well when no other processes use CUDA during your training.

        force_outputs_fp32: Whether to cast outputs to fp32.

        gpu_margin_mem_ratio: The ratio of GPU remaining memory (after the first forward-backward)
            which will be used by optimizer.
            This argument will be ignored when ``placement_policy`` is not "auto".

        chunk_search_range: The range of chunk size to search.
            The actual search range will be from
            ``max(min_chunk_size, max_param_size)`` to ``max(min_chunk_size, max_param_size) + chunk_search_range``.

        chunk_search_n_grids: The number of intervals in the search range.

        min_chunk_size: The minimum size for a chunk.

        initial_scale: The initial dynamic loss scale value.

        min_scale: The minimum dynamic loss scaling value.

        growth_factor: The multiplication factor for increasing loss scale.

        backoff_factor: The multiplication factor for decreasing loss scale.

        growth_interval: The number of steps to increase loss scale when no overflow occurs.

        hysteresis: The number of overflows before decreasing loss scale.

        max_scale: The maximum dynamic loss scaling value.

    .. _colossalai.nn.optimizer.CPUAdam:
        https://colossalai.readthedocs.io/en/latest/colossalai/colossalai.nn.optimizer.cpu_adam.html

    .. _colossalai.nn.optimizer.HybridAdam:
        https://colossalai.readthedocs.io/en/latest/colossalai/colossalai.nn.optimizer.hybrid_adam.html

        strategy_name = "colossalai"
    """

    def __init__(
        self,
        use_chunk: bool = True,
        chunk_size: Optional[int] = None,
        enable_distributed_storage: bool = True,
        placement_policy: str = "auto",
        force_outputs_fp32: bool = False,
        gpu_margin_mem_ratio: float = 0.0,
        chunk_search_range: int = 64 * 1024**2,
        chunk_search_n_grids: int = 1024,
        min_chunk_size: Optional[int] = None,
        initial_scale: float = 2**32,
        min_scale: float = 1,
        growth_factor: float = 2,
        backoff_factor: float = 0.5,
        growth_interval: int = 1000,
        hysteresis: int = 2,
        max_scale: float = 2**32,
        accelerator: Optional["pl.accelerators.accelerator.Accelerator"] = None,
        parallel_devices: Optional[List[torch.device]] = None,
        cluster_environment: Optional[ClusterEnvironment] = None,
        checkpoint_io: Optional[CheckpointIO] = None,
        precision_plugin: Optional[PrecisionPlugin] = None,
    ) -> None:
        if not _COLOSSALAI_AVAILABLE:
            raise MisconfigurationException(
                "To use the `ColossalAIStrategy`, please install `colossalai` first. "
                "Download `colossalai` by consulting `https://colossalai.org/download`."
            )

        super().__init__(
            accelerator=accelerator,
            parallel_devices=parallel_devices,
            cluster_environment=cluster_environment,
            checkpoint_io=checkpoint_io,
            precision_plugin=precision_plugin,
        )

        self.use_chunk = use_chunk
        self.chunk_size = chunk_size
        self.enable_distributed_storage = enable_distributed_storage
        self.placement_policy = placement_policy
        self.force_outputs_fp32 = force_outputs_fp32
        self.gpu_margin_mem_ratio = gpu_margin_mem_ratio
        self.chunk_size_search_kwargs = {
            "search_range": chunk_search_range,
            "n_grids": chunk_search_n_grids,
            "min_chunk_size": min_chunk_size,
        }
        self.amp_kwargs = {
            "initial_scale": initial_scale,
            "min_scale": min_scale,
            "growth_factor": growth_factor,
            "backoff_factor": backoff_factor,
            "growth_interval": growth_interval,
            "hysteresis": hysteresis,
            "max_scale": max_scale,
        }
        self._num_nodes = 1
        self._logger = get_dist_logger()

    @property
    def root_device(self) -> torch.device:
        if self.parallel_devices is not None:
            return self.parallel_devices[self.local_rank]
        return get_current_device()

    @property
    def lightning_module(self) -> Optional["pl.LightningModule"]:
        if isinstance(self.model, ZeroDDP):
            return self.model.module.lightning_module
        return super().lightning_module

    @property
    def handles_gradient_accumulation(self) -> bool:
        """Whether the plugin handles gradient accumulation internally."""
        return True

    def setup_distributed(self):
        if not gpc.is_initialized(ParallelMode.GLOBAL):
            disable_existing_loggers()
            gpc.init_global_dist(
                rank=self.global_rank,
                world_size=self.world_size,
                backend="nccl",
                host=self.cluster_environment.main_address,
                port=self.cluster_environment.main_port,
            )
            gpc.set_device(self.local_rank)

    def model_sharded_context(self):
        """Provide hook to create modules in a distributed aware context. This is useful for when we'd like to
        shard the model instantly, which is useful for extremely large models which can save memory and
        initialization time.

        Returns: Model parallel context.
        """

        class ModelShardedContext(ColoInitContext):
            def _post_init_method(self, module: torch.nn.Module, *args, **kwargs):
                if getattr(module, "_colossalai_module", False) is True:
                    return
                super()._post_init_method(module, *args, **kwargs)
                module._colossalai_module = True

        return ModelShardedContext()

    def setup_precision_plugin(self) -> None:
        super().setup_precision_plugin()
        is_training = self.lightning_module.trainer and self.lightning_module.trainer.training
        if is_training:
            if len(self.optimizers) > 1:
                raise MisconfigurationException("`ColossalAIStrategy` only supports single Optimizer now.")
            optimizer = self.optimizers[0]
            if not isinstance(optimizer, (CPUAdam, HybridAdam)):
                raise MisconfigurationException(
                    "`ColossalAIStrategy` only supports `colossalai.nn.optimizer.CPUAdam` "
                    "and `colossalai.nn.optimizer.HybridAdam` as its optimizer."
                )
        pl_module = self.model
        process_group = ProcessGroup()
        if not hasattr(pl_module, "_colossalai_zero"):
            if self.use_chunk:
                chunk_size = self.chunk_size or ChunkManager.search_chunk_size(
                    self.model, **self.chunk_size_search_kwargs
                )
            else:
                chunk_size = None
            chunk_manager = ChunkManager(
                chunk_size,
                process_group,
                self.enable_distributed_storage,
                GeminiManager.get_default_device(self.placement_policy),
            )
            gemini_manager = GeminiManager(self.placement_policy, chunk_manager)
            assert isinstance(self.model, (pl.LightningModule, _LightningPrecisionModuleWrapperBase))
            model = _LightningModuleWrapperBase(self.model)
            self.model = ZeroDDP(model, gemini_manager, self.force_outputs_fp32)
            pl_module._colossalai_zero = [self.model]
        else:
            self.model = pl_module._colossalai_zero[0]
        if is_training:
            self.optimizers = [
                ZeroOptimizer(optimizer, self.model, gpu_margin_mem_ratio=self.gpu_margin_mem_ratio, **self.amp_kwargs)
            ]

    def setup(self, trainer: "pl.Trainer") -> None:
        if not isinstance(self.accelerator, CUDAAccelerator):
            raise MisconfigurationException(
                "`ColossalAIStrategy` is only supported on `CUDAAccelerator`, "
                f"but `{self.accelerator.__class__.__name__}` is used."
            )

        if trainer.accumulate_grad_batches > 1:
            raise MisconfigurationException(
                "ColossalAI does not support gradient accumulation now. Please set `accumulate_grad_batches` to 1."
            )

        accumulation_scheduler = trainer.accumulation_scheduler
        if accumulation_scheduler.epochs != [0]:
            raise MisconfigurationException(
                "ColossalAI currently does not support different `accumulate_grad_batches` at different epochs."
            )

        if not isinstance(self.precision_plugin, ColossalAIPrecisionPlugin):
            raise MisconfigurationException("`ColossalAIStrategy` is only compatible with `ColossalAIPrecisionPlugin`.")

        self.accelerator.setup(trainer)
        self.setup_optimizers(trainer)
        self.setup_precision_plugin()
        self.model_to_device()

    def model_to_device(self) -> None:
        pl_module = self.lightning_module
        pl_module._device = self.root_device
        for child in pl_module.modules():
            if child is not pl_module and not getattr(child, "_colossalai_module", False):
                child.to(self.root_device)

    def teardown(self) -> None:
        return

    def optimizer_step(self, optimizer, opt_idx: int, closure, model=None, **kwargs: Any) -> Any:
        model = model or self.lightning_module
        return self.precision_plugin.optimizer_step(model, optimizer, opt_idx, closure, **kwargs)

    def lightning_module_state_dict(self, only_rank_0: bool = False):
        """Returns a dictionary containing a whole state of the module. But all the tensors in the dictionary are
        detached from their parameters and located in cpu memory.

        Args:
            only_rank_0: If True, only process rank 0 gets the correct dictionary.
                Otherwise, all processes get the same dictionary.
        """
        org_dict = self.model.state_dict(only_rank_0=only_rank_0)

        children = list(self.model.named_children())
        assert len(children) == 1
        prefix, child = children[0]
        prefix += "."
        assert child is self.lightning_module

        mapping_dict = dict()
        for key in org_dict.keys():
            mapping_dict[key] = key.replace(prefix, "")  # remove "_forward_module." from the key

        return {mapping_dict[key]: value for key, value in org_dict.items()}

    def validation_step(self, *args: Any, **kwargs: Any) -> Optional[STEP_OUTPUT]:
        assert self.model is not None
        with self.precision_plugin.val_step_context():
            return self.model(*args, **kwargs)

    def test_step(self, *args: Any, **kwargs: Any) -> Optional[STEP_OUTPUT]:
        assert self.model is not None
        with self.precision_plugin.test_step_context():
            return self.model(*args, **kwargs)

    def predict_step(self, *args: Any, **kwargs: Any) -> STEP_OUTPUT:
        assert self.model is not None
        with self.precision_plugin.predict_step_context():
            return self.model(*args, **kwargs)

    @classmethod
    def register_strategies(cls, strategy_registry: Dict) -> None:
        strategy_registry.register("colossalai", cls, description="Default ColossalAI Strategy")
