# Copyright 2022 The OFA-Sys Team.
# All rights reserved.
# This source code is licensed under the Apache 2.0 license
# found in the LICENSE file in the root directory.

from dataclasses import dataclass, field
from typing import Optional
import json
import logging
import torch
import torch.distributed as dist

from fairseq.tasks import register_task
from fairseq.utils import move_to_cuda

from tasks.base_task import BaseTask, BaseTaskConfig
from data.audio_data.audio_text_retrieval_dataset import AudioTextRetrievalDataset
from utils.data_utils import new_islice, all_gather
from metrics import Recall

logger = logging.getLogger(__name__)


@dataclass
class AudioTextRetrievalConfig(BaseTaskConfig):
    valid_file: Optional[str] = field(
        default=None,
        metadata={"help": ""},
    )
    use_template: bool = False


@register_task("audio_text_retrieval", dataclass=AudioTextRetrievalConfig)
class AudioTextRetrievalTask(BaseTask):
    def __init__(self, cfg, dictionary):
        super().__init__(cfg, dictionary)
        self.metric = Recall()
        self.text_ids = None
        self.texts = None

    def load_dataset(self, split, epoch=1, **kwargs):
        dataset = super().load_dataset(split, epoch, **kwargs)

        if self.text_ids is None and self.cfg.valid_file is not None:
            self.text_ids = []
            self.texts = []
            for text_id, text_list in json.load(open(self.cfg.valid_file)).items():
                for text in text_list:
                    self.text_ids.append(int(text_id))
                    self.texts.append(text)
            self.text_ids = torch.tensor(self.text_ids).cuda()

        self.datasets[split] = AudioTextRetrievalDataset(
            split,
            dataset,
            self.bpe,
            self.dict,
            max_src_length=self.cfg.max_src_length,
            max_duration=self.cfg.max_duration,
            feature_encoder_spec=self.cfg.feature_encoder_spec
        )

    @torch.no_grad()
    def begin_valid_epoch(self, epoch, model, subset):
        assert self.text_ids is not None and self.texts is not None
        model.eval()

        dataset = self.datasets[subset]
        text_cnt = len(self.text_ids)

        if dist.is_initialized():
            slice_id = dist.get_rank()
            slice_count = dist.get_world_size()
        else:
            slice_id = 0
            slice_count = 1
        batch_sampler = new_islice(range(text_cnt), slice_id, text_cnt, slice_count)
        start_idx = batch_sampler[0]
        end_idx = batch_sampler[-1] + 1

        text_logits_list = []
        for i in range(start_idx, end_idx, 50):
            samples_list = []
            for text in self.texts[i:min(i+50, end_idx)]:
                text = "This is a sound of " + text if self.cfg.use_template else text
                item_tuple = (0, None, text, 1)
                sample = dataset.__getitem__(0, item_tuple)
                samples_list.append(sample)
            samples = dataset.collater(samples_list)
            samples = move_to_cuda(samples)
            src_tokens = samples["net_input"]["src_tokens"]
            text_logits = model(src_tokens=src_tokens, encoder_type='text')
            text_logits_list.append(text_logits)

        text_logits = torch.cat(text_logits_list, dim=0)
        text_logits = all_gather(text_logits) if dist.is_initialized() else text_logits
        self.metric.initialize(self.text_ids, text_logits)

    @torch.no_grad()
    def valid_step(self, sample, model, criterion, is_dummy_batch):
        loss = 0
        sample_size = len(sample['id'])
        logging_output = {'nsentences': 1, 'ntokens': 1}
        if not is_dummy_batch:
            model.eval()
            self.eval_step(model, sample)
        return loss, sample_size, logging_output

    @torch.no_grad()
    def eval_step(self, model, sample):
        src_audios = sample["net_input"]["src_audios"]
        audio_padding_masks = sample["net_input"]["audio_padding_masks"]
        audio_ids = torch.tensor(sample['id']).to(src_audios.device)
        audio_logits = model(src_audios=src_audios, audio_padding_masks=audio_padding_masks, encoder_type='audio')
        self.metric.compute(audio_ids, audio_logits)

    @torch.no_grad()
    def merge_results(self, output_predict=False):
        stats =  self.metric.merge_results(output_predict=output_predict)
        for key in list(stats.keys()):
            if key.startswith('img'):
                stats[key.replace('img', 'audio')] = stats[key]
                del stats[key]
        return stats