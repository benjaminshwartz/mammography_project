import numpy as np
import torch
from torch import nn
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader
from torchvision import transforms
import os
import pandas as pd
import pickle
import random
import itertools
import time
from datetime import datetime, timedelta
from torchmetrics import ROC
from torchmetrics.classification import BinaryROC
from matplotlib import pyplot as plt
import boto3 as boto

# modules important for multiprocessing

from torch.nn.parallel import DistributedDataParallel as DDP


class Trainer():
    def __init__(
        self,
        model: torch.nn.Module,
        optimizer: torch.optim.Optimizer,
        loss_fn: torch.nn,
        gpu_id: int,
        save_interval: int,
        metric_interval: int,
        train_data: DataLoader,
        validation_data: DataLoader = None,
        test_data: DataLoader = None
    ) -> None:
        ############ GPU RUNNING ########
        self.model = model.to(gpu_id)
        self.model = DDP(self.model, device_ids=[gpu_id])

        ############ CHANGING DATALOADER ############

        # self.model = model
        self.train_data = train_data
        self.optimizer = optimizer
        self.loss_fn = loss_fn
        self.gpu_id = gpu_id
        self.save_interval = save_interval
        self.metric_interval = metric_interval
        self.validation_data = validation_data
        self.test_data = test_data
        self.curr_preds_lst = []
        self.curr_labels_lst = []
        self.s3 = boto.client('s3')

    def _run_batch(self, batch_tensor: torch.tensor, batch_labels: torch.tensor):
        self.optimizer.zero_grad()

        predicted_output = self.model(batch_tensor).to(self.gpu_id)
        self.curr_preds_lst.append(predicted_output)
        # print(f'SHAPE OF PREDICTED OUTPUT: {predicted_output.shape}')
        batch_labels = torch.reshape(
            batch_labels, (predicted_output.shape[0], 2))
        self.curr_labels_lst.append(batch_labels)

        # print(f'SHAPE OF LABELS: {batch_labels.shape}')
        loss = self.loss_fn(predicted_output, batch_labels.long())
        loss.backward()
        self.optimizer.step()

    def _run_epoch(self, epoch: int):
        self.model.train()
        self.curr_preds_lst = []
        self.curr_labels_lst = []
        print(f'\t[GPU {self.gpu_id}] Epoch {epoch}')
        # i = 1
        # all = len(self.train_data)

        i = 0
        for batch_tensor, batch_labels in self.train_data:
            # print(f'\t{i}/{len(self.train_data)}')
            # i += 1

            ########## GPU RUNNING ##############
            batch_tensor = batch_tensor.to(self.gpu_id)
            batch_labels = batch_labels.to(self.gpu_id)

            self._run_batch(batch_tensor, batch_labels.float())
            i += 1
            if i % 20 == 0:
                print(f'Have run {i} batches')

    # TODO delete checkpoint file after upload to s3 bucket

    def _save_checkpoint(self, epoch: int):
        checkpoint = self.model.module.state_dict()
        #torch.save(checkpoint, CHECKPOINT_PATH)
        pickle.dump(checkpoint, open(f'checkpoint_{epoch}.pt', 'wb'))
        self.s3.upload_file(f'checkpoint_{epoch}.pt', 'mammographydata',
                            f'DataSet/checkpointmodels/checkpoint_{epoch}.pt')
        print(f'\tModel Saved at Epoch {epoch}')

    def train(self, num_epochs: int, sv_roc: bool = False):
        self.model.share_memory()
        for epoch in range(1, num_epochs + 1):


            self._run_epoch(epoch)

            if self.save_interval > 0 and epoch % self.save_interval == 0 and self.gpu_id == 0:
                self._save_checkpoint(epoch)
            elif epoch == num_epochs:  # save last model
                self._save_checkpoint(epoch)
            if self.metric_interval > 0 and epoch % self.metric_interval == 0:
                print("\tTrain Metrics (Training Data):")
                self.evaluate(None, sv_roc=sv_roc)
                if self.test_data != None:
                    print("\tTest Metrics:")
                    self.evaluate(self.test_data)
                    self.model.train()
            elif epoch == num_epochs:  # Evaluate final model
                print("\tTrain Metrics:")
                self.evaluate(None, sv_roc=sv_roc)
                if self.test_data != None:
                    print("\tTest Metrics:")
                    self.evaluate(self.test_data)
                    self.model.train()

    def evaluate(self, dataloader: DataLoader = None, sv_roc=False):

        with torch.no_grad():
            self.model.eval()
            cumulative_loss = 0
            left_cumulative_loss = 0
            right_cumulative_loss = 0
            num_correct = 0
            num_one_off_correct = 0
            num_correct_left = 0
            num_correct_one_off_left = 0
            num_correct_right = 0
            num_correct_one_off_right = 0
            total = 0
            # num_batches = len(dataloader)
            # num_batches = len(predicted_output)
            # all_preds = []  # torch.tensor([]).to(self.gpu_id)

            if dataloader is None:
                predicted_output = torch.vstack(self.curr_preds_lst)
                labels = torch.vstack(self.curr_labels_lst).long()
            else:
                pred_lst = []
                label_lst = []
                for batch_tensor, batch_labels in dataloader:

                    batch_tensor = batch_tensor.to(self.gpu_id)
                    # we want batch_labels.shape = B, 5, 2
                    # we want predicted_output = B, 5, 2
                    batch_labels = batch_labels.to(self.gpu_id).long()

                    predicted_output = self.model(batch_tensor).to(self.gpu_id)
                    pred_lst.append(predicted_output)

                    batch_labels = batch_labels.long()
                    batch_labels = torch.reshape(
                        batch_labels, (predicted_output.shape[0], 2)).to(self.gpu_id)
                    label_lst.append(batch_labels)

                predicted_output = torch.vstack(pred_lst)
                labels = torch.vstack(label_lst).long()

            left_preds = predicted_output[:, :, 0].to(self.gpu_id)
            right_preds = predicted_output[:, :, 1].to(self.gpu_id)

            left_labels = labels[:, 0].to(self.gpu_id)
            right_labels = labels[:, 1].to(self.gpu_id)

            cumulative_loss += self.loss_fn(predicted_output, labels)
            left_cumulative_loss += self.loss_fn(left_preds, left_labels)
            right_cumulative_loss += self.loss_fn(
                right_preds, right_labels)

            if sv_roc:
                # TODO fix roc curve
                pass
            else:
                pass

            # print(f'THIS IS THE RIGHT LABEL: {right_labels}')
            # print(f'THIS IS THE RIGHT PREDICTED LABEL: {right_preds}')
            # print('##################################')
            # print(f'THIS IS THE LEFT LABEL: {left_labels}')
            # print(f'THIS IS THE LEFT PREDICTED LABEL: {left_preds}')
            # print('++++++++++++++++++++++++++++++++++++++++++')
            total += len(left_labels)
            # print(f'TOTAL NUMBER: {total}')

            # num_correct_left += (torch.argmax(left_preds, dim=0)
            #                      == torch.argmax(left_labels, dim=0)).sum().item()

            # num_correct_right += (torch.argmax(right_preds, dim=0)
            #                       == torch.argmax(right_labels, dim=0)).sum().item()
            left_positions = torch.argmax(left_preds, dim=1)
            right_positions = torch.argmax(right_preds, dim=1)
            # print(f'LEFT PREDS: {left_positions}')
            # print(f'RIGHT PREDS: {right_positions}')
            # print('-------------------------------------')
            # print(f'LEFT LABELS: {left_labels}')
            # print(f'RIGHT LABELS: {right_labels}')
            # print('--------------------------------------')

            correct_left_lst = (left_positions == left_labels)
            correct_right_lst = (right_positions == right_labels)

            one_off_left_lst = torch.isclose(left_positions, left_labels,
                                             rtol=0, atol=1, equal_nan=False)
            one_off_right_lst = torch.isclose(right_positions, right_labels,
                                              rtol=0, atol=1, equal_nan=False)

            num_correct_left = correct_left_lst.sum().item()
            num_correct_one_off_left = one_off_left_lst.sum().item()

            # print(f'NUMBER CORRECT STATED LEFT: {num_correct_left}')

            num_correct_right = correct_right_lst.sum().item()
            num_correct_one_off_right = one_off_right_lst.sum().item()

            num_binary_correct_left = (
                left_positions >= 1) == (left_labels >= 1)
            num_binary_correct_right = (
                right_positions >= 1) == (right_labels >= 1)

            # print(f'NUMBER CORRECT STATED RIGHT: {num_correct_right}')
            # print('##################################')

            # for i in range(len(left_positions)):
            #     if (left_positions[i] == left_labels[i]) and (right_positions[i] == right_labels[i]):
            #         num_correct += 1

            num_correct = torch.logical_and(
                correct_left_lst, correct_right_lst).sum().item()

            num_one_off_correct = torch.logical_and(
                one_off_left_lst, one_off_right_lst).sum().item()

            num_binary_correct = torch.logical_and(
                num_binary_correct_left, num_binary_correct_right).sum().item()

            # num_correct += ((torch.argmax(right_preds, dim=0) == right_labels) and (torch.argmax(left_preds, dim=0))).sum().item()

            # print(f'TOTAL NUM CORRECT: {num_correct}')
            # print('!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!')

            ########################################
            # for batch_tensor, batch_labels in dataloader:

            #     ####### GPU RUNNING#####
            #     batch_tensor = batch_tensor.to(self.gpu_id)
            #     # we want batch_labels.shape = B, 5, 2
            #     # we want predicted_output = B, 5, 2
            #     batch_labels = batch_labels.to(self.gpu_id).long()

            #     predicted_output = self.model(batch_tensor).to(self.gpu_id)
            #     left_preds = predicted_output[:, :, 0].to(self.gpu_id)
            #     right_preds = predicted_output[:, :, 1].to(self.gpu_id)

            #     batch_labels = batch_labels.long()
            #     batch_labels = torch.reshape(
            #         batch_labels, (predicted_output.shape[0], 2)).to(self.gpu_id)
            #     left_labels = batch_labels[:, 0].to(self.gpu_id)
            #     right_labels = batch_labels[:, 1].to(self.gpu_id)

            #     cumulative_loss += self.loss_fn(predicted_output, batch_labels)
            #     left_cumulative_loss += self.loss_fn(left_preds, left_labels)
            #     right_cumulative_loss += self.loss_fn(
            #         right_preds, right_labels)

            #     if sv_roc:
            #         # TODO fix roc curve
            #         pass
            #     else:
            #         pass
            #     # print(f'THIS IS THE RIGHT LABEL: {right_labels}')
            #     print(f'THIS IS THE RIGHT PREDICTED LABEL: {right_preds}')
            #     # print('##################################')
            #     # print(f'THIS IS THE LEFT LABEL: {left_labels}')
            #     print(f'THIS IS THE LEFT PREDICTED LABEL: {left_preds}')
            #     # print('++++++++++++++++++++++++++++++++++++++++++')
            #     total += len(left_labels)
            #     print(f'TOTAL NUMBER: {total}')

            #     # num_correct_left += (torch.argmax(left_preds, dim=0)
            #     #                      == torch.argmax(left_labels, dim=0)).sum().item()

            #     # num_correct_right += (torch.argmax(right_preds, dim=0)
            #     #                       == torch.argmax(right_labels, dim=0)).sum().item()
            #     left_positions = torch.argmax(left_preds, dim=1)
            #     right_positions = torch.argmax(right_preds, dim=1)
            #     print(f'LEFT PREDS: {left_positions}')
            #     print(f'RIGHT PREDS: {right_positions}')
            #     print('-------------------------------------')
            #     print(f'LEFT LABELS: {left_labels}')
            #     print(f'RIGHT LABELS: {right_labels}')
            #     print('--------------------------------------')

            #     num_correct_left += (left_positions ==
            #                          left_labels).sum().item()
            #     num_correct_one_off_left += torch.isclose(
            #         left_positions, left_labels, rtol=0, atol=1, equal_nan=False).sum().item()

            #     print(f'NUMBER CORRECT STATED LEFT: {num_correct_left}')

            #     num_correct_right += (right_positions ==
            #                           right_labels).sum().item()
            #     num_correct_one_off_right += torch.isclose(
            #         right_positions, right_labels, rtol=0, atol=1, equal_nan=False).sum().item()

            #     print(f'NUMBER CORRECT STATED RIGHT: {num_correct_right}')
            #     print('##################################')

            #     for i in range(len(left_positions)):
            #         if (left_positions[i] == left_labels[i]) and (right_positions[i] == right_labels[i]):
            #             num_correct += 1

            #     # num_correct += ((torch.argmax(right_preds, dim=0) == right_labels) and (torch.argmax(left_preds, dim=0))).sum().item()

            #     print(f'TOTAL NUM CORRECT: {num_correct}')
            #     print('!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!')

            # loss = cumulative_loss/num_batches
            # left_loss = left_cumulative_loss / num_batches
            # right_loss = right_cumulative_loss / num_batches
            # accuracy = num_correct/total
            # accuracy_left = num_correct_left/total
            # one_off_left = num_correct_one_off_left/total
            # accuracy_right = num_correct_right/total
            # one_off_right = num_correct_one_off_right/total

            # print(
            #     f'\t\tOverall Loss: {loss} = {cumulative_loss}/{num_batches}')
            # print(
            #     f'\t\tLeft Loss: {left_loss} = {left_cumulative_loss}/{num_batches}')
            # print(
            #     f'\t\tRight Loss: {right_loss} = {right_cumulative_loss}/{num_batches}')

            # print(f'\t\tOverall Accuracy: {accuracy} = {num_correct}/{total}')
            # print(
            #     f'\t\tLeft Accuracy: {accuracy_left} = {num_correct_left}/{total}')
            # print(
            #     f'\t\tLeft One-Off Accuracy: {one_off_left} = {num_correct_one_off_left}/{total}')
            # print(
            #     f'\t\tRight Accuracy: {accuracy_right} = {num_correct_right}/{total}')
            # print(
            #     f'\t\tLeft One-Off Accuracy: {one_off_right} = {num_correct_one_off_right}/{total}')

            ########################################

            loss = cumulative_loss
            left_loss = left_cumulative_loss
            right_loss = right_cumulative_loss
            accuracy = num_correct/total
            binary_acc = num_binary_correct/total
            one_off_accuracy = num_one_off_correct/total
            accuracy_left = num_correct_left/total
            binary_acc_left = num_binary_correct_left/total
            one_off_left = num_correct_one_off_left/total
            accuracy_right = num_correct_right/total
            binary_acc_right = num_binary_correct_right/total
            one_off_right = num_correct_one_off_right/total

            print(
                f'\t\tOverall Loss: {loss}')
            print(
                f'\t\tLeft Loss: {left_loss}')
            print(
                f'\t\tRight Loss: {right_loss}')
            print('------------------------------------------------')

            print(f'\t\tOverall Accuracy: {accuracy} = {num_correct}/{total}')
            print(
                f'\t\tOverall Binary Accuracy: {binary_acc} = {num_binary_correct}/{total}')
            print(
                f'\t\tOverall One-Off Accuracy: {one_off_accuracy} = {num_one_off_correct}/{total}')
            print('------------------------------------------------')
            print(
                f'\t\tLeft Accuracy: {accuracy_left} = {num_correct_left}/{total}')
            print(
                f'\t\tLeft Binary Accuracy: {binary_acc_left} = {num_binary_correct_left}/{total}')
            print(
                f'\t\tLeft One-Off Accuracy: {one_off_left} = {num_correct_one_off_left}/{total}')
            print('------------------------------------------------')
            print(
                f'\t\tRight Accuracy: {accuracy_right} = {num_correct_right}/{total}')
            print(
                f'\t\tRight Binary Accuracy: {binary_acc_right} = {num_binary_correct_right}/{total}')
            print(
                f'\t\tRight One-Off Accuracy: {one_off_right} = {num_correct_one_off_right}/{total}')

            if sv_roc:
                # TODO fix save roc
                # Trainer.save_roc(all_preds, all_labels)
                pass

        self.model.train()

    class Tester:
        def __init__(
            self,
            model: torch.nn.Module,
            loss_fn: torch.nn = None,
            gpu_id: int = 0,
        ) -> None:
            self.loss_fn = loss_fn or torch.nn.CrossEntropyLoss()
            self.gpu_id = gpu_id
            # self.model = model.to(self.gpu_id)
            self.model = model

        def evaluate(self, dataloader: DataLoader, sv_roc=False):
            with torch.no_grad():
                self.model.eval()
                cumulative_loss = 0
                num_correct = 0
                total = 0
                num_batches = len(dataloader)
                # all_preds = torch.tensor([]).to(self.gpu_id)
                # all_labels = torch.tensor([]).to(self.gpu_id)
                all_preds = torch.tensor([])
                all_labels = torch.tensor([])

                for batch_tensor, batch_labels in dataloader:

                    # batch_tensor = batch_tensor.to(self.gpu_id)
                    batch_tensor = batch_tensor
                    # check batch labels type
                    # batch_tensor = batch_tensor.to(self.gpu_id)
                # we want batch_labels.shape = B, 5, 2
                # we want predicted_output = B, 5, 2
                # batch_labels = batch_labels.to(self.gpu_id).long()
                batch_labels = batch_labels.long()
                batch_labels = torch.reshape(batch_labels, (2,))
                left_labels = batch_labels[0]
                right_labels = batch_labels[1]

                predicted_output = self.model(batch_tensor)
                left_preds = predicted_output[0]
                right_preds = predicted_output[1]

                cumulative_loss += self.loss_fn(predicted_output, batch_labels)
                left_cumulative_loss += self.loss_fn(left_preds, left_labels)
                right_cumulative_loss += self.loss_fn(
                    right_preds, right_labels)

                if sv_roc:
                    softmax = nn.Softmax(dim=1)
                    all_preds = torch.cat(
                        (all_preds, (softmax(predicted_output)[:, 1])))
                    all_labels = torch.cat((all_labels, batch_labels))

                # assuming decision boundary to be 0.5
                total += batch_labels.size(0)

#                 num_correct_left += (torch.argmax(left_preds, dim=0)
#                                      == torch.argmax(left_labels, dim=0)).sum().item()

#                 num_correct_right += (torch.argmax(right_preds, dim=0)
#                                       == torch.argmax(right_labels, dim=0)).sum().item()

            num_correct_left += (torch.argmax(left_preds, dim=0)
                                 == left_labels).sum().item()

            num_correct_right += (torch.argmax(right_preds,
                                  dim=0) == right_labels).sum().item()

            loss = cumulative_loss/num_batches
            left_loss = left_cumulative_loss / num_batches
            right_loss = right_cumulative_loss / num_batches
            accuracy = num_correct/total
            half = total/2
            accuracy_left = num_correct_left/half
            accuracy_right = num_correct_right/half

            print(
                f'\t\tOverall Loss: {loss} = {cumulative_loss}/{num_batches}')
            print(
                f'\t\tLeft Loss: {left_loss} = {left_cumulative_loss}/{num_batches}')
            print(
                f'\t\tRight Loss: {right_loss} = {right_cumulative_loss}/{num_batches}')

            print(f'\t\tAccuracy: {accuracy} = {num_correct}/{total}')
            print(
                f'\t\tLeft Accuracy: {accuracy_left} = {num_correct_left}/{half}')
            print(
                f'\t\tRight Accuracy: {accuracy_right} = {num_correct_right}/{half}')

    # TODO: fix for (B,2,5) prediction
    @ staticmethod
    def save_roc(all_preds, all_labels):
        roc = ROC(task="binary", thresholds=20)
        roc = BinaryROC(thresholds=100)
        all_preds = all_preds.cpu()
        all_labels = all_labels.cpu().int()
        fpr, tpr, thresholds = roc(all_preds, all_labels)
        plt.plot([0, 1], [0, 1], linestyle='dashed')
        plt.plot(fpr, tpr)
        plt.ylabel('True Positive Rate')
        plt.xlabel('False Positive Rate')
        plt.savefig('ROC.png')
