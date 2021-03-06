import argparse
import os
from fairseq.models.roberta import RobertaModel
import torch
from fairseq import tasks
import torch.nn.functional as F
from fairseq.data import Dictionary
from my_utils import visualize_input_grad
import pdb

def get_tokens(line, roberta, task):
    tokens = line.strip().split('\t')
    if task == "RTE":
        sent1, sent2 = tokens[1], tokens[2]
        tokens = roberta.encode(sent1, sent2)
    elif task == "MRPC":
        sent1, sent2 = tokens[3], tokens[4]
        tokens = roberta.encode(sent1, sent2)
    elif task == "CoLA":
        sent = tokens[1]
        tokens = roberta.encode(sent)
    elif task == "SST-2":
        sent = tokens[1]
        tokens = roberta.encode(sent)
    elif task == "STS-B":
        sent1, sent2 = tokens[7], tokens[8]
        tokens = roberta.encode(sent1, sent2)
    elif task == "QQP":
        sent1, sent2 = tokens[1], tokens[2]
        tokens = roberta.encode(sent1, sent2)
    elif task == "QNLI":
        sent1, sent2 = tokens[1], tokens[2]
        tokens = roberta.encode(sent1, sent2)
    elif task == "WNLI":
        sent1, sent2 = tokens[1], tokens[2]
        tokens = roberta.encode(sent1, sent2)
    elif task == "MNLI-m":
        sent1, sent2 = tokens[-2], tokens[-1]
        tokens = roberta.encode(sent1, sent2)
    elif task == "MNLI-mm":
        sent1, sent2 = tokens[-2], tokens[-1]
        tokens = roberta.encode(sent1, sent2)
    elif task == "AX":
        sent1, sent2 = tokens[-2], tokens[-1]
        tokens = roberta.encode(sent1, sent2)
    else:
        print("Task {} undefined".format(task))
        exit()
    return tokens


def bytes_to_unicode():
    """
    Returns list of utf-8 byte and a corresponding list of unicode strings.
    The reversible bpe codes work on unicode strings.
    This means you need a large # of unicode characters in your vocab if you want to avoid UNKs.
    When you're at something like a 10B token dataset you end up needing around 5K for decent coverage.
    This is a signficant percentage of your normal, say, 32K bpe vocab.
    To avoid that, we want lookup tables between utf-8 bytes and unicode strings.
    And avoids mapping to whitespace/control characters the bpe code barfs on.
    """
    bs = list(range(ord("!"), ord("~")+1))+list(range(ord("??"), ord("??")+1))+list(range(ord("??"), ord("??")+1))
    cs = bs[:]
    n = 0
    for b in range(2**8):
        if b not in bs:
            bs.append(b)
            cs.append(2**8+n)
            n += 1
    cs = [chr(n) for n in cs]
    return dict(zip(bs, cs))


def decode(tokens, decoder, byte_decoder):
    text = ''.join([decoder[token.item()] for token in tokens])
    text = bytearray([byte_decoder[c] for c in text]).decode('utf-8', errors='replace')
    return text

def load_dictionary(filename):
    """Load the dictionary from the filename

    Args:
        filename (str): the filename
    """
    dictionary = Dictionary.load(filename)
    dictionary.add_symbol('<mask>')
    return dictionary


def get_entropy(attn_mat, tokens):
    # attn_mat: batch_size x shape
    # attn_mat = attn_mat.view(attn_mat.size(0), -1)
    # batch_size x num_heads x tok_len x tok_len
    bsize, nheads, toklen, _ = attn_mat.size()
    # attn_mat = attn_mat.transpose(1,2).contiguous().view(bsize, toklen, -1) / nheads
    # weighted_likelihood = - attn_mat * torch.log(torch.clamp(attn_mat, min=1e-10))
    # mask = (tokens != 1).float().unsqueeze(2).cuda()
    # total_entropy = torch.sum(mask * weighted_likelihood)
    cls_head_attn = attn_mat[:,:,0,:].contiguous()#.view(bsize, -1) / nheads
    total_entropy = torch.sum(cls_head_attn * torch.clamp(cls_head_attn, min=1e-10)).item()
    return total_entropy


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--chk-dir", default="checkpoints", type=str)
    parser.add_argument("--chk-fname", default="checkpoint_best.pt", type=str)
    parser.add_argument("--task", default="sentence_prediction", type=str)
    parser.add_argument("--dset", default="CoLA", type=str)
    parser.add_argument("--test-path", default="glue_data")
    parser.add_argument("--out-path", default="glue-test")
    parser.add_argument("--num", default=0, type=int)
    parser.add_argument("--batch-size", default=2, type=int)
    parser.add_argument("--num-classes", default=2, type=int)
    parser.add_argument("--max-positions", default=512, type=int)
    parser.add_argument("--regression-target", default=False, type=bool)
    parser.add_argument("--dataset-impl", default=None)
    parser.add_argument("--init-token", default=0, type=int)
    parser.add_argument("--separator-token", default=2, type=int)
    parser.add_argument("--no-shuffle", default=True, type=int)
    parser.add_argument("--seed", default=1, help="Shall not be used. Placeholder")
    parser.add_argument("--truncate-sequence", default=False)
    parser.add_argument("--attn-layer-idx", default=0, type=int)
    parser.add_argument("--gpu", default="0", type=str)
    args = parser.parse_args()
    print(args)
    os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"  # see issue #152
    os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu

    bin_task = "MNLI" if "MNLI" in args.dset or args.dset == "AX" else args.dset
    args.data = bin_task+"-bin"

    roberta = RobertaModel.from_pretrained(
        args.chk_dir,
        checkpoint_file=args.chk_fname,
        data_name_or_path=bin_task+"-bin"
    )

    # untrained = torch.load('pretrained/roberta.large/model.pt')
    # roberta.model.load_state_dict(untrained['model'], strict=False)
    # pdb.set_trace()

    label_fn = lambda label: roberta.task.label_dictionary.string(
        [label + roberta.task.target_dictionary.nspecial]
    )
    ncorrect, nsamples = 0, 0
    roberta.to('cuda')
    roberta.eval()
    if not os.path.exists(args.out_path):
        os.makedirs(args.out_path)

    if not os.path.exists(os.path.join(args.out_path, "scores")):
        os.makedirs(os.path.join(args.out_path, "scores"))

    task = tasks.setup_task(args)

    split = "valid"

    task.load_dataset(split)
    itr = task.get_batch_iterator(
        dataset=task.dataset(split),
        max_tokens=4400,
        max_sentences=args.batch_size,
        max_positions=512,
        ignore_invalid_inputs=False,
        required_batch_size_multiple=1,
        seed=1,
    ).next_epoch_itr(shuffle=False)
    total_entropy, total_attns = 0, 0
    total_corr, total_samples = 0, 0
    scores_list = []

    out_fs = open("visualizations/{}-dev-input-grad.html".format(args.dset), "w+")

    total_norm_grad, total_tokens = 0, 0

    for batch in itr:
        tokens = batch['net_input']['src_tokens']
        labels = batch['target'].view(-1).cuda()
        ids = batch['id']

        if args.dset == "STS-B":
            logits = roberta.predict('sentence_classification_head', tokens, return_logits=True)
            scores_list.append(logits)
            n_corr = torch.sum(torch.abs(logits-labels))
            loss = F.mse_loss(
                logits,
                labels,
                reduction="sum",
            )
        else:
            logit = roberta.predict('sentence_classification_head', tokens, return_logits=True)
            loss = F.nll_loss(
                F.log_softmax(logit, dim=-1, dtype=torch.float32),
                labels,
                reduction="sum",
            )

            prediction = logit.argmax(dim=1).detach()
            n_corr = torch.sum(prediction.view(-1)==labels.view(-1).cuda()).item()
            total_corr += n_corr
            scores_list.append(logit)
        loss.backward()
        embed_grad = roberta.model.decoder.sentence_encoder.token_embed.grad.clone().detach()
        embed_grad_norm = torch.norm(embed_grad, p=2, dim=2)
        embed_grad_norm_normalize = embed_grad_norm / torch.sum(embed_grad_norm, 1, keepdim=True)

        sent_grad_norm = torch.norm(embed_grad.view(embed_grad.size(0), -1), p=2, dim=1)
        total_norm_grad += torch.sum(sent_grad_norm).item()

        for tidx in range(tokens.size(0)):
            valid_len = batch['net_input']['src_lengths'][tidx]
            sent_dec = roberta.decode_as_token(tokens[tidx][:valid_len])
            if isinstance(sent_dec, list):
                sent_print = ""
                word_arr = []
                for sent in sent_dec:
                    sent_print +=  "<start>\t" + sent + "\t<end>"
                    word_arr += ['"START"'] + ['%s' % (word) for word in sent.split('\t')] + ['"END"']
            else:
                sent_print = "<start>\t" + sent_dec + "\t<end>"
                word_arr = ['"START"'] + ['%s' % (word) for word in sent_dec.split('\t')] + ['"END"']
            valid_grad = embed_grad_norm[tidx, :valid_len]
            grad_print = "\t".join(["%.2e"%g.item() for g in valid_grad])
            # print(sent_print+"\n"+grad_print)
            # print(tidx, len(sent_print.split("\t")), valid_len)

            grad_norm_arr = [ g.item() for g in 0.9-0.4 * (1 - valid_grad / torch.max(valid_grad))]
            # pdb.set_trace()
            assert(len(word_arr) == len(grad_norm_arr))

            vis_line = visualize_input_grad.colorize_with_label(ids[tidx], word_arr, grad_norm_arr, labels[tidx].item(),
                                                                prediction[tidx])
            out_fs.write(vis_line)
            out_fs.write("\n")


        grad_entropy = -torch.sum(embed_grad_norm_normalize * torch.log(torch.clamp(embed_grad_norm_normalize, 1e-10)))
        # batch_size x num_heads x tok_len x tok_len
        # for attn in attn_list:
        #     batch_sum_entropy = get_entropy(attn, tokens)
        #     total_entropy += batch_sum_entropy
        #     # number_of_heads x token_lens
        #     total_attns += tokens.size(0)
        total_entropy += grad_entropy
        total_attns += tokens.size(0)
        total_samples += tokens.size(0)
        total_tokens += torch.sum(batch['net_input']['src_lengths'])
    scores_mat = torch.cat(scores_list, 0)
    out_fs.close()

    # torch.save({"model_name": args.chk_dir, "scores": scores_mat}, os.path.join(args.out_path, "scores", "%s_scores_%d.pt"%(args.dset, args.num)))
    print("Metric: {}".format(total_corr / float(total_samples)))
    print("Mean Entropy on dev set: {}".format(total_entropy/total_attns))
    print("Mean gradient on dev set: {}".format(total_norm_grad / total_samples))

