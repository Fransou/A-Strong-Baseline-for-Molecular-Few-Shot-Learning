import torch
import torch.nn as nn
from omegaconf import OmegaConf
from functools import partial
import os
import inspect

# add parentdir
currentdir = os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))
parentdir = os.path.dirname(currentdir)
os.sys.path.insert(0, parentdir)

from mhnfs.hopfield.modules import Hopfield
from mhnfs.initialization import init_weights


# Mappings
activation_function_mapping = {
    "relu": nn.ReLU(),
    "selu": nn.SELU(),
    "sigmoid": nn.Sigmoid(),
}

dropout_mapping = {"relu": nn.Dropout, "selu": nn.AlphaDropout}


# Modules
class EncoderBlock(nn.Module):
    """
    Fully connected molecule encoder block.
    - Takes molecular descriptors, e.g., ECFPs and RDKit fps as inputs
    - returns a molecular representation
    """

    def __init__(self, cfg: OmegaConf):
        super(EncoderBlock, self).__init__()

        # Input layer
        self.dropout = dropout_mapping[cfg.model.encoder.activation](
            cfg.model.encoder.regularization.input_dropout
        )
        self.fc = nn.Linear(
            cfg.model.encoder.input_dim, cfg.model.encoder.number_hidden_neurons
        )
        self.act = activation_function_mapping[cfg.model.encoder.activation]

        # Hidden layer
        self.hidden_linear_layers = nn.ModuleList([])
        self.hidden_dropout_layers = nn.ModuleList([])
        self.hidden_activations = nn.ModuleList([])

        for _ in range(cfg.model.encoder.number_hidden_layers):
            self.hidden_dropout_layers.append(
                dropout_mapping[cfg.model.encoder.activation](
                    cfg.model.encoder.regularization.dropout
                )
            )
            self.hidden_linear_layers.append(
                nn.Linear(
                    cfg.model.encoder.number_hidden_neurons,
                    cfg.model.encoder.number_hidden_neurons,
                )
            )
            self.hidden_activations.append(
                activation_function_mapping[cfg.model.encoder.activation]
            )

        # Output layer
        self.dropout_o = dropout_mapping[cfg.model.encoder.activation](
            cfg.model.encoder.regularization.dropout
        )
        self.fc_o = nn.Linear(
            cfg.model.encoder.number_hidden_neurons,
            cfg.model.associationSpace_dim,
        )
        self.act_o = activation_function_mapping[cfg.model.encoder.activation]

        # Initialization
        encoder_initialization = partial(init_weights, cfg.model.encoder.activation)
        self.apply(encoder_initialization)

    def forward(self, molecule_representation: torch.Tensor) -> torch.Tensor:
        # Input layer
        x = self.dropout(molecule_representation)
        x = self.fc(x)
        x = self.act(x)

        # Hidden layer
        for hidden_dropout, hidden_layer, hidden_activation_function in zip(
            self.hidden_dropout_layers,
            self.hidden_linear_layers,
            self.hidden_activations,
        ):
            x = hidden_dropout(x)
            x = hidden_layer(x)
            x = hidden_activation_function(x)

        # Output layer
        x = self.dropout_o(x)
        x = self.fc_o(x)
        x = self.act_o(x)

        return x


class ContextModule(nn.Module):
    """
    Allows for mutual information sharing.
    Enriches the query and support set embeddings with context by associating a query or
    support set molecule with the context set, i.e., large set of training molecules:
    - The context set can be seen as an external memory
    - For a given molecule embedding, a Modern Hopfield Network retrieves a representa-
      tion from the external memory

    Since we have to retrieve representations for all query and support set molecules we
    stack all embeddings together and perform a "batch-retrieval".
    """

    def __init__(self, cfg: OmegaConf):
        super(ContextModule, self).__init__()

        self.cfg = cfg

        self.hopfield = Hopfield(
            input_size=self.cfg.model.associationSpace_dim,
            hidden_size=cfg.model.hopfield.dim_QK,
            stored_pattern_size=self.cfg.model.associationSpace_dim,
            pattern_projection_size=self.cfg.model.associationSpace_dim,
            output_size=self.cfg.model.associationSpace_dim,
            num_heads=self.cfg.model.hopfield.heads,
            scaling=self.cfg.model.hopfield.beta,
            dropout=self.cfg.model.hopfield.dropout,
        )

        # Initialization
        hopfield_initialization = partial(init_weights, "linear")
        self.hopfield.apply(hopfield_initialization)

    def forward(
        self,
        query_embedding: torch.Tensor,
        support_actives_embedding: torch.Tensor,
        support_inactives_embedding: torch.Tensor,
        context_set_embedding: torch.Tensor,
    ) -> tuple:
        """
        inputs:
        - query; torch.tensor;
          dim: [batch-size, 1, initial-embedding-dimension]
            * e.g.: [512, 1, 1024]
            * initial-embedding-dimension: defined by molecule encoder block
        - active support set molecules; torch.tensor;
          dim: [batch-size, active-padding-dim, initial-embedding-dimension]
          * e.g.: [512, 9, 1024]
        - inactive support set molecules; torch.tensor;
          dim: [batch-size, inactive-padding-dim, initial-embedding-dimension]
          * e.g.: [512, 11, 1024]
        - context set molecules; torch.tensor;
          dim: [1, number-of-context-molecules, initial-embedding-dimension]
          * e.g.: [1, 512, 1024]

        return:
        tuple which includes the updated representations for query, active, and inactive
        support set molecules:
        (query, active support set molecules, inactive support set molecules)
        """
        # Stack embeddings together to perform a "batch-retrieval"
        s = torch.cat(
            (query_embedding, support_actives_embedding, support_inactives_embedding),
            dim=1,
        )
        s_flattend = s.reshape(1, s.shape[0] * s.shape[1], s.shape[2])

        # Retrieval
        s_h = self.hopfield((context_set_embedding, s_flattend, context_set_embedding))

        # Combine retrieval with skip connection
        s_updated = s_flattend + s_h
        s_updated_inputShape = s_updated.reshape(
            s.shape[0], s.shape[1], s.shape[2]
        )  # reshape tensor back to input shape

        query_embedding = s_updated_inputShape[:, 0, :]
        query_embedding = torch.unsqueeze(query_embedding, 1)

        # Split query, active and inactive support set embeddings
        padding_size_actives = support_actives_embedding.shape[1]

        support_actives_embedding = s_updated_inputShape[
            :, 1 : (padding_size_actives + 1), :
        ]
        support_inactives_embedding = s_updated_inputShape[
            :, (padding_size_actives + 1) :, :
        ]

        return query_embedding, support_actives_embedding, support_inactives_embedding


class LayerNormalizingBlock(nn.Module):
    """
    Layernorm-block which scales/transforms the representations for query, ac-
    tive, and inactive support set molecules.
    """

    def __init__(self, cfg: OmegaConf):
        super(LayerNormalizingBlock, self).__init__()

        self.cfg = cfg

        if cfg.model.layerNormBlock.usage:
            self.layernorm_query = nn.LayerNorm(
                cfg.model.associationSpace_dim,
                elementwise_affine=cfg.model.layerNormBlock.affine,
            )
            self.layernorm_support_actives = nn.LayerNorm(
                cfg.model.associationSpace_dim,
                elementwise_affine=cfg.model.layerNormBlock.affine,
            )
            self.layernorm_support_inactives = nn.LayerNorm(
                cfg.model.associationSpace_dim,
                elementwise_affine=cfg.model.layerNormBlock.affine,
            )

    def forward(
        self,
        query_embedding: torch.Tensor,
        support_actives_embedding: torch.Tensor,
        support_inactives_embedding: torch.Tensor,
    ) -> tuple:
        """
        inputs:
        - query; torch.tensor;
          dim: [batch-size, 1, embedding-dim]
            * e.g.: [512, 1, 1024]
        - active support set molecules; torch.tensor;
          dim: [batch-size, active-padding-dim, embedding-dim]
          * e.g.: [512, 9, 1024]
        - inactive support set molecules; torch.tensor;
          dim: [batch-size, inactive-padding-dim, initial-embedding-dim]
          * e.g.: [512, 11, 1024]

        return:
        tuple which includes the updated representations for query, active, and inactive
        support set molecules:
        (query, active support set molecules, inactive support set molecules)
        """

        # Layer normalization
        # Since the layernorm operations are optional the module just updates represen-
        # tations if the the referring option is set in the config.
        if self.cfg.model.layerNormBlock.usage:
            query_embedding = self.layernorm_query(query_embedding)
            support_actives_embedding = self.layernorm_support_actives(
                support_actives_embedding
            )
            if support_inactives_embedding is not None:
                support_inactives_embedding = self.layernorm_support_inactives(
                    support_inactives_embedding
                )
        return query_embedding, support_actives_embedding, support_inactives_embedding


class CrossAttentionModule(nn.Module):
    """
    The cross-attention module allows for information sharing between query and support
    set molecules.

    Altae-Tran et al. [1] showed that representations can be enriched by making the
    query molecule aware of the support set molecules and making the support set mole-
    cules aware of each other and of the query molecule. We enable information sharing
    with a transformer.

    Overview of the cross-attention module:
    1) The query and support set molecules are concatenated such that one joint matrix
       emerges which includes both query and support set molecules.
    2) The joint matrix is fed into a transformer
       - Self-attention enables information sharing between query and support set mole-
         cules

    [1] Altae-Tran, H., Ramsundar, B., Pappu, A. S., & Pande, V. (2017). Low data drug
        discovery with one-shot learning. ACS central science, 3(4), 283-293.
    """

    def __init__(self, cfg: OmegaConf):
        super(CrossAttentionModule, self).__init__()

        self.cfg = cfg

        transformer_encoder_layer = torch.nn.TransformerEncoderLayer(
            d_model=(
                self.cfg.model.associationSpace_dim
                + self.cfg.model.transformer.activity_embedding_dim
            ),
            nhead=self.cfg.model.transformer.number_heads,
            dim_feedforward=self.cfg.model.transformer.dim_forward,
            dropout=self.cfg.model.transformer.dropout,
        )
        self.transformer = torch.nn.TransformerEncoder(
            transformer_encoder_layer,
            num_layers=self.cfg.model.transformer.num_layers,
        )

    def forward(
        self,
        query_embedding: torch.Tensor,
        support_actives_embedding: torch.Tensor,
        support_inactives_embedding: torch.Tensor,
        # support_set_actives_size: torch.Tensor,
        # support_set_inactives_size: torch.Tensor,
        support_actives_mask: torch.Tensor,
        support_inactives_mask: torch.Tensor,
    ) -> tuple:
        """
        inputs:
        - query; torch.tensor;
          dim: [batch-size, 1, embedding-dim]
            * e.g.: [512, 1, 1024]
        - active support set molecules; torch.tensor;
          dim: [batch-size, active-padding-dim, embedding-dim]
          * e.g.: [512, 9, 1024]
        - inactive support set molecules; torch.tensor;
          dim: [batch-size, inactive-padding-dim, initial-embedding-dim]
          * e.g.: [512, 11, 1024]
        - number of active molecules in support set; torch.tensor;
          dim: [batch-size]
        - number of inactive molecules in support set; torch.tensor;
          dim: [batch-size]

        return:
        tuple which includes the updated representations for query, active, and inactive
        support set molecules:
        (query, active support set molecules, inactive support set molecules)
        query_embedding, support_actives_embedding, support_inactives_embedding
        """

        # Embedding dim of query and support set molecules
        embedding_dim = support_actives_embedding.shape[2]

        # Add activity encoding to representations
        # Activity encoding:
        # - active: 1
        # - inactive: -1
        # - unknown (query): 0
        query_embedding = torch.cat(
            [
                query_embedding,
                torch.zeros_like(
                    query_embedding[
                        :, :, : self.cfg.model.transformer.activity_embedding_dim
                    ]
                ),
            ],
            dim=2,
        )

        support_actives_embedding = torch.cat(
            [
                support_actives_embedding,
                torch.ones_like(
                    support_actives_embedding[
                        :, :, : self.cfg.model.transformer.activity_embedding_dim
                    ]
                ),
            ],
            dim=2,
        )

        support_inactives_embedding = torch.cat(
            [
                support_inactives_embedding,
                (-1.0)
                * torch.ones_like(
                    support_inactives_embedding[
                        :, :, : self.cfg.model.transformer.activity_embedding_dim
                    ]
                ),
            ],
            dim=2,
        )

        # Concatenate query and support set molecules
        s = torch.cat(
            [query_embedding, support_actives_embedding, support_inactives_embedding],
            dim=1,
        )

        # Create padding mask
        padding_mask = torch.cat(
            [
                torch.tensor([False] * support_actives_mask.shape[0])
                .reshape(-1, 1)
                .to(self.cfg.system.ressources.device),  # query molecules
                # .to("cpu"),  # query molecules
                support_actives_mask,  # active support set molecules
                support_inactives_mask,  # inactive support set molecules
            ],
            dim=1,
        ).bool()

        # Run transformer and update representations
        s = torch.transpose(s, 0, 1)
        s_h = self.transformer(s, src_key_padding_mask=padding_mask)
        s = torch.transpose(s, 0, 1)
        s_h = torch.transpose(s_h, 0, 1)
        s_updated = s + s_h

        # Split representations into query, active, and inactive support set molecules
        query_embedding = s_updated[:, 0, :embedding_dim]
        query_embedding = torch.unsqueeze(query_embedding, 1)
        support_actives_embedding = s_updated[
            :, 1 : (support_actives_embedding.shape[1] + 1), :embedding_dim
        ]
        support_inactives_embedding = s_updated[
            :, (support_actives_embedding.shape[1] + 1) :, :embedding_dim
        ]

        return query_embedding, support_actives_embedding, support_inactives_embedding


def SimilarityModule(
    query_embedding: torch.Tensor,
    support_set_embeddings: torch.Tensor,
    padding_mask: torch.Tensor,
    support_set_size: torch.Tensor,
    cfg: OmegaConf,
) -> torch.Tensor:
    """
    The similarity module builds the activity prediction for the query molecule from a
    weighted sum over the support set labels. Pair-wise similarity values between query
    and support set molecules are used as weights for the weighted sum.

    Since the similarity module is applied twice within the MHNfs model - once for the
    active and once for the inactive support set molecules, the support_set_embeddings
    here mean ether active or inactive support set molecule embeddings.

    inputs:
    - query; torch.tensor;
      dim: [batch-size, 1, embedding-dimension]
        * e.g.: [512, 1, 1024]
    - support set molecules; torch.tensor;
      dim: [batch-size, padding-dim, embedding-dimension]
        * e.g.: [512, 9, 1024]
    - padding mask; torch.tensor; boolean
      dim: [batch-size, padding-dim]
        * e.g.: [512, 9]
    - support set size; torch.tensor;
      dim: [batch-size]
        * e.g.: [512]
    """

    # Optional L2-norm
    if cfg.model.similarityModule.l2Norm:
        query_embedding_div = torch.unsqueeze(
            query_embedding.pow(2).sum(dim=2).sqrt(), 2
        )
        query_embedding_div[query_embedding_div == 0] = 1
        support_set_embeddings_div = torch.unsqueeze(
            support_set_embeddings.pow(2).sum(dim=2).sqrt(), 2
        )
        support_set_embeddings_div[support_set_embeddings_div == 0] = 1

        query_embedding = query_embedding / query_embedding_div
        support_set_embeddings = support_set_embeddings / support_set_embeddings_div

    # Compute similarity values
    similarities = query_embedding @ torch.transpose(support_set_embeddings, 1, 2)
    # dim:
    # [batch-size, 1, padding-dim] =
    # [batch-size, 1, emb-dim] x [batch-size, emb-dim, padding-dim]

    # Padding mask
    mask = (
        (padding_mask.bool()).float().unsqueeze(1)
    )

    # Compute similarity values while ignoring padding artefacts
    similarities = similarities * mask
    similarity_sums = similarities.sum(
        dim=2
    )  # For every query molecule: Sum over support set molecules

    # Scaling
    if cfg.model.similarityModule.scaling == "1/N":
        stabilizer = torch.tensor(1e-8).float()
        similarity_sums = (
            1 / (2.0 * support_set_size.reshape(-1, 1) + stabilizer) * similarity_sums
        )
    if cfg.model.similarityModule.scaling == "1/sqrt(N)":
        stabilizer = torch.tensor(1e-8).float()
        similarity_sums = (
            1
            / (2.0 * torch.sqrt(support_set_size.reshape(-1, 1).float()) + stabilizer)
            * similarity_sums
        )

    return similarity_sums


if __name__ == "__main__":
    debug_context_module()
