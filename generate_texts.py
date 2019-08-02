import torch
import torch.nn.functional as F
import pytorch_transformers
import os
import tokenization_bert
import argparse
from tqdm import trange
from pytorch_transformers import GPT2LMHeadModel

os.environ["CUDA_VISIBLE_DEVICES"] = "0,1,2,3"  # 此处设置程序使用哪些显卡



def _is_chinese_char(char):
    """Checks whether CP is the codepoint of a CJK character."""
    # This defines a "chinese character" as anything in the CJK Unicode block:
    #   https://en.wikipedia.org/wiki/CJK_Unified_Ideographs_(Unicode_block)
    #
    # Note that the CJK Unicode block is NOT all Japanese and Korean characters,
    # despite its name. The modern Korean Hangul alphabet is a different block,
    # as is Japanese Hiragana and Katakana. Those alphabets are used to write
    # space-separated words, so they are not treated specially and handled
    # like the all of the other languages.
    cp = ord(char)
    if ((cp >= 0x4E00 and cp <= 0x9FFF) or  #
            (cp >= 0x3400 and cp <= 0x4DBF) or  #
            (cp >= 0x20000 and cp <= 0x2A6DF) or  #
            (cp >= 0x2A700 and cp <= 0x2B73F) or  #
            (cp >= 0x2B740 and cp <= 0x2B81F) or  #
            (cp >= 0x2B820 and cp <= 0x2CEAF) or
            (cp >= 0xF900 and cp <= 0xFAFF) or  #
            (cp >= 0x2F800 and cp <= 0x2FA1F)):  #
        return True

    return False

def top_k_top_p_filtering(logits, top_k=0, top_p=0.0, filter_value=-float('Inf')):
    """ Filter a distribution of logits using top-k and/or nucleus (top-p) filtering
        Args:
            logits: logits distribution shape (vocabulary size)
            top_k > 0: keep only top k tokens with highest probability (top-k filtering).
            top_p > 0.0: keep the top tokens with cumulative probability >= top_p (nucleus filtering).
                Nucleus filtering is described in Holtzman et al. (http://arxiv.org/abs/1904.09751)
        From: https://gist.github.com/thomwolf/1a5a29f6962089e871b94cbd09daf317
    """
    assert logits.dim() == 1  # batch size 1 for now - could be updated for more but the code would be less clear
    top_k = min(top_k, logits.size(-1))  # Safety check
    if top_k > 0:
        # Remove all tokens with a probability less than the last token of the top-k
        indices_to_remove = logits < torch.topk(logits, top_k)[0][..., -1, None]
        logits[indices_to_remove] = filter_value

    if top_p > 0.0:
        sorted_logits, sorted_indices = torch.sort(logits, descending=True)
        cumulative_probs = torch.cumsum(F.softmax(sorted_logits, dim=-1), dim=-1)

        # Remove tokens with cumulative probability above the threshold
        sorted_indices_to_remove = cumulative_probs > top_p
        # Shift the indices to the right to keep also the first token above the threshold
        sorted_indices_to_remove[..., 1:] = sorted_indices_to_remove[..., :-1].clone()
        sorted_indices_to_remove[..., 0] = 0

        indices_to_remove = sorted_indices[sorted_indices_to_remove]
        logits[indices_to_remove] = filter_value
    return logits


def sample_sequence(model, length, context, num_samples=1, temperature=1, top_k=0, top_p=0.0, is_xlnet=False,
                    device='cpu'):
    context = torch.tensor(context, dtype=torch.long, device=device)
    context = context.unsqueeze(0).repeat(num_samples, 1)
    generated = context
    with torch.no_grad():
        for _ in trange(length):

            inputs = {'input_ids': generated}
            if is_xlnet:
                # XLNet is a direct (predict same token, not next token) and bi-directional model by default
                # => need one additional dummy token in the input (will be masked), attention mask and target mapping (see model docstring)
                input_ids = torch.cat((generated, torch.zeros((1, 1), dtype=torch.long, device=device)), dim=1)
                perm_mask = torch.zeros((1, input_ids.shape[1], input_ids.shape[1]), dtype=torch.float, device=device)
                perm_mask[:, :, -1] = 1.0  # Previous tokens don't see last token
                target_mapping = torch.zeros((1, 1, input_ids.shape[1]), dtype=torch.float, device=device)
                target_mapping[0, 0, -1] = 1.0  # predict last token
                inputs = {'input_ids': input_ids, 'perm_mask': perm_mask, 'target_mapping': target_mapping}

            outputs = model(
                **inputs)  # Note: we could also use 'past' with GPT-2/Transfo-XL/XLNet (cached hidden-states)
            next_token_logits = outputs[0][0, -1, :] / temperature
            filtered_logits = top_k_top_p_filtering(next_token_logits, top_k=top_k, top_p=top_p)
            next_token = torch.multinomial(F.softmax(filtered_logits, dim=-1), num_samples=1)
            generated = torch.cat((generated, next_token.unsqueeze(0)), dim=1)
    return generated


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--device', default='1,2,3,4', type=str, required=False)
    parser.add_argument('--length', default=-1, type=int, required=False)
    parser.add_argument('--batch_size', default=1, type=int, required=False)
    parser.add_argument('--nsamples', default=10, type=int, required=False)
    parser.add_argument('--temperature', default=1, type=float, required=False)
    parser.add_argument('--topk', default=8, type=int, required=False)
    parser.add_argument('--topp', default=0, type=float, required=False)
    parser.add_argument('--model_config', default='config/model_config_small.json', type=str, required=False)
    parser.add_argument('--tokenizer_path', default='cache/vocab_small.txt', type=str, required=False)
    parser.add_argument('--model_path', default='model/final_model', type=str, required=False)
    parser.add_argument('--save_path', default='generated/', type=str, required=False)
    parser.add_argument('--articles_per_title', default=5, type=int, required=False)
    parser.add_argument('--titles', default='萧炎', type=str, required=False)

    args = parser.parse_args()
    print(args)

    os.environ["CUDA_VISIBLE_DEVICES"] = args.device  # 此处设置程序使用哪些显卡
    length = args.length
    batch_size = args.batch_size
    nsamples = args.nsamples
    temperature = args.temperature
    topk = args.topk
    topp = args.topp
    titles = args.title.split()  # 列表，里面每个元素是一个生成的标题
    articles_per_title = args.articles_per_title  # 这里定义一个标题生成多少篇文章
    save_path = args.save_path  # 设置存到哪

    device = "cuda" if torch.cuda.is_available() else "cpu"

    tokenizer = tokenization_bert.BertTokenizer(vocab_file=args.tokenizer_path)
    model_config = pytorch_transformers.GPT2Config.from_json_file(args.model_config)
    model = GPT2LMHeadModel(config=model_config).from_pretrained(args.model_path)
    model.to(device)
    model.eval()

    if not os.path.exists(save_path):
        os.mkdir(save_path)
    if length == -1:
        length = model.config.n_ctx // 2
    elif length > model.config.n_ctx:
        raise ValueError("Can't get samples longer than window size: %s" % model.config.n_ctx)

    for i, title in enumerate(titles):
        for j in range(articles_per_title):
            with open(save_path + str(i * j), 'w') as f:
                context_tokens = tokenizer.convert_tokens_to_ids(tokenizer.tokenize(title))
                generated = 0
                out = sample_sequence(
                    model=model, length=length,
                    context=context_tokens,
                    temperature=temperature, top_k=topk, top_p=topp, device=device
                )
                out = out.tolist()
                generated += 1
                text = tokenizer.convert_ids_to_tokens(out[0])
                for i, item in enumerate(text):
                    if item == '[MASK]':
                        text[i] = ''
                    if item == '[CLS]' or item == '[SEP]':
                        text[i] = '\n'

                print("=" * 40 + " SAMPLE " + str(generated) + " " + "=" * 40)
                text = ''.join(text).replace('##', '').strip()
                print(text)
                f.write(text)
                print("=" * 80)


if __name__ == '__main__':
    main()