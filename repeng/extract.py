import dataclasses
import typing
import warnings

import gguf
import numpy as np
from sklearn.decomposition import PCA
import torch
from transformers import PreTrainedModel, PreTrainedTokenizerBase
import tqdm

from .control import ControlModel, model_layer_list


@dataclasses.dataclass
class DatasetEntry:
    positive: str
    negative: str


@dataclasses.dataclass
class ControlVector:
    model_type: str
    directions: dict[int, np.ndarray]

    @classmethod
    def train(
        cls,
        model: "PreTrainedModel | ControlModel",
        tokenizer: PreTrainedTokenizerBase,
        dataset: list[DatasetEntry],
        **kwargs,
    ) -> "ControlVector":
        dirs = read_representations(
            model,
            tokenizer,
            dataset,
            **kwargs,
        )
        return cls(model_type=model.config.model_type, directions=dirs)

    def export_gguf(self, path: str):
        """
        Export a trained ControlVector to a llama.cpp .gguf file.
        Note: This file can't be used with llama.cpp yet. WIP!

        ```python
        vector = ControlVector.train(...)
        vector.export_gguf("path/to/write/vector.gguf")
        ```
        ```
        """

        arch = "controlvector"
        writer = gguf.GGUFWriter(path, arch)
        writer.add_string(f"{arch}.model_hint", self.model_type)
        writer.add_uint32(f"{arch}.layer_count", len(self.directions))
        for layer in self.directions.keys():
            writer.add_tensor(f"direction.{layer}", self.directions[layer])
        writer.write_header_to_file()
        writer.write_kv_data_to_file()
        writer.write_tensors_to_file()
        writer.close()

    def _helper_combine(
        self, other: "ControlVector", other_coeff: float
    ) -> "ControlVector":
        if self.model_type != other.model_type:
            warnings.warn(
                "Trying to add vectors with mismatched model_types together, this may produce unexpected results."
            )

        model_type = self.model_type
        directions: dict[int, np.ndarray] = {}
        for layer in self.directions:
            directions[layer] = self.directions[layer]
        for layer in other.directions:
            other_layer = other_coeff * other.directions[layer]
            if layer in directions:
                directions[layer] = directions[layer] + other_layer
            else:
                directions[layer] = other_layer
        return ControlVector(model_type=model_type, directions=directions)

    def __add__(self, other: "ControlVector") -> "ControlVector":
        if not isinstance(other, ControlVector):
            raise TypeError(
                f"Unsupported operand type(s) for +: 'ControlVector' and '{type(other).__name__}'"
            )
        return self._helper_combine(other, 1)

    def __sub__(self, other: "ControlVector") -> "ControlVector":
        if not isinstance(other, ControlVector):
            raise TypeError(
                f"Unsupported operand type(s) for -: 'ControlVector' and '{type(other).__name__}'"
            )
        return self._helper_combine(other, -1)

    def __neg__(self) -> "ControlVector":
        directions: dict[int, np.ndarray] = {}
        for layer in self.directions:
            directions[layer] = -self.directions[layer]
        return ControlVector(model_type=self.model_type, directions=directions)

    def __mul__(self, other: int | float | np.int_ | np.float_) -> "ControlVector":
        directions: dict[int, np.ndarray] = {}
        for layer in self.directions:
            directions[layer] = other * self.directions[layer]
        return ControlVector(model_type=self.model_type, directions=directions)

    def __rmul__(self, other: int | float | np.int_ | np.float_) -> "ControlVector":
        return self.__mul__(other)

    def __truediv__(self, other: int | float | np.int_ | np.float_) -> "ControlVector":
        return self.__mul__(1 / other)


def read_representations(
    model: "PreTrainedModel | ControlModel",
    tokenizer: PreTrainedTokenizerBase,
    inputs: list[DatasetEntry],
    hidden_layers: typing.Iterable[int] | None = None,
    batch_size: int = 32,
) -> dict[int, np.ndarray]:
    """
    Extract the representations based on the contrast dataset.
    """

    if not hidden_layers:
        hidden_layers = range(-1, -model.config.num_hidden_layers, -1)

    # normalize the layer indexes if they're negative
    n_layers = len(model_layer_list(model))
    hidden_layers = [i if i >= 0 else n_layers + i for i in hidden_layers]

    # the order is [positive, negative, positive, negative, ...]
    train_strs = [s for ex in inputs for s in (ex.positive, ex.negative)]

    layer_hiddens, sample_mapping = batched_get_hiddens(
        model, tokenizer, train_strs, hidden_layers, batch_size
    )

    # Initialize dictionary to hold differences between positive and negative pairs for each layer
    relative_layer_hiddens = {}

    for layer in hidden_layers:
        # Create lists to hold the hidden states for positive and negative samples
        positives = []
        negatives = []
        
        # Iterate over each sample and classify it as positive or negative based on its label in sample_mapping
        for idx, sample_id in enumerate(sample_mapping):

            if sample_mapping[sample_id] == 'positive':
                positives.append(layer_hiddens[layer][idx])
            else:
                negatives.append(layer_hiddens[layer][idx])
          
        positives_array = np.array(positives)
        negatives_array = np.array(negatives)
        
        # Ensure there is a matching number of positive and negative samples before subtraction
        min_len = min(len(positives_array), len(negatives_array))
        relative_layer_hiddens[layer] = np.array(positives)[:min_len] - np.array(negatives)[:min_len]

    # get directions for each layer using PCA
    directions: dict[int, np.ndarray] = {}
    for layer in tqdm.tqdm(hidden_layers):
        assert layer_hiddens[layer].shape[0] == len(inputs) * 2

        # fit layer directions
        train = np.vstack(
            relative_layer_hiddens[layer]
            - relative_layer_hiddens[layer].mean(axis=0, keepdims=True)
        )
        pca_model = PCA(n_components=1, whiten=False).fit(train)
        # shape (n_features,)
        directions[layer] = pca_model.components_.astype(np.float32).squeeze(axis=0)

        # calculate sign
        projected_hiddens = project_onto_direction(
            layer_hiddens[layer], directions[layer]
        )

        # order is [positive, negative, positive, negative, ...]
        positive_smaller_mean = np.mean(
            [
                projected_hiddens[i] < projected_hiddens[i + 1]
                for i in range(0, len(inputs) * 2, 2)
            ]
        )
        positive_larger_mean = np.mean(
            [
                projected_hiddens[i] > projected_hiddens[i + 1]
                for i in range(0, len(inputs) * 2, 2)
            ]
        )

        if positive_smaller_mean > positive_larger_mean:  # type: ignore
            directions[layer] *= -1

    return directions


def batched_get_hiddens(
    model,
    tokenizer,
    dataset: list[DatasetEntry],
    hidden_layers: list[int],
    batch_size: int,
) -> tuple[dict[int, np.ndarray], dict[str, str]]:
    """
    Using the given model and tokenizer, pass the pre-generated completions through the model and get the hidden
    states for each layer in `hidden_layers` for the last token.

    Returns a tuple containing:
    - A dictionary from `hidden_layers` layer id to a numpy array of shape `(n_inputs, hidden_dim)`
    - A dictionary mapping the sampleId to the corresponding label (correct/incorrect)
    """
    batched_dataset = [
        dataset[p : p + batch_size] for p in range(0, len(dataset), batch_size)
    ]
    hidden_states = {layer: [] for layer in hidden_layers}
    sample_mapping = {}

    with torch.no_grad():
        for batch in tqdm.tqdm(batched_dataset):
            batch_completions = [entry.completion for entry in batch]
            input_ids = tokenizer(batch_completions, padding=True, return_tensors="pt").to(model.device)
            outputs = model(
                input_ids,
                output_hidden_states=True,
            )

            for entry in batch:
                sample_mapping[entry.sampleId] = entry.label

            # pull the layers for the 
            for layer in hidden_layers:
                # if not indexing from end, account for embedding hiddens
                hidden_idx = layer + 1 if layer >= 0 else layer
                for batch in outputs.hidden_states[hidden_idx]:
                    states = batch[-1, :].squeeze().cpu().numpy()
                    hidden_states[layer].append(states)
            del outputs
    
    return {k: np.vstack(v) for k, v in hidden_states.items()}, sample_mapping


def project_onto_direction(H, direction):
    """Project matrix H (n, d_1) onto direction vector (d_2,)"""
    mag = np.linalg.norm(direction)
    assert not np.isinf(mag)
    return (H @ direction) / mag
