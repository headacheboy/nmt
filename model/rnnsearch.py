# rnnsearch.py
# author: Playinf
# email: playinf@stu.xmu.edu.cn

import nn
import ops
import numpy
import theano

from search import beam, select_nbest


def gru_encoder(cell, inputs, mask, initial_state=None, dtype=None):
    if not isinstance(cell, nn.rnn_cell.gru_cell):
        raise ValueError("only gru_cell is supported")

    if isinstance(inputs, (list, tuple)):
        raise ValueError("inputs must be a tensor, not list or tuple")

    def loop_fn(inputs, mask, state):
        mask = mask[:, None]
        output, next_state = cell(inputs, state)
        next_state = (1.0 - mask) * state + mask * next_state
        return next_state

    if initial_state is None:
        batch = inputs.shape[1]
        state_size = cell.state_size
        initial_state = theano.tensor.zeros([batch, state_size], dtype=dtype)

    seq = [inputs, mask]
    states = ops.scan(loop_fn, seq, [initial_state])

    return states


def encoder(cell, inputs, mask, initial_state=None, dtype=None, scope=None):
    with ops.variable_scope(scope or "encoder"):
        with ops.variable_scope("forward"):
            fd_states = gru_encoder(cell, inputs, mask, initial_state, dtype)
        with ops.variable_scope("backward"):
            inputs = inputs[::-1]
            mask = mask[::-1]
            bd_states = gru_encoder(cell, inputs, mask, initial_state, dtype)
            bd_states = bd_states[::-1]

    return fd_states, bd_states


# precompute mapped attention states to speed up decoding
# attention_states: [time_steps, batch, input_size]
# outputs: [time_steps, batch, attn_size]
def map_attention_states(attention_states, input_size, attn_size, scope=None):
    with ops.variable_scope(scope or "attention"):
        mapped_states = nn.linear(attention_states, [input_size, attn_size],
                                  False, scope="attention_w")

    return mapped_states


def attention(query, mapped_states, state_size, attn_size, attention_mask=None,
              scope=None):
    with ops.variable_scope(scope or "attention"):
        mapped_query = nn.linear(query, [state_size, attn_size], False,
                                 scope="query_w")

        mapped_query = mapped_query[None, :, :]
        hidden = theano.tensor.tanh(mapped_query + mapped_states)

        score = nn.linear(hidden, [attn_size, 1], False, scope="attention_v")
        score = score.reshape([score.shape[0], score.shape[1]])

        exp_score = theano.tensor.exp(score)

        if attention_mask is not None:
            exp_score = exp_score * attention_mask

        alpha = exp_score / theano.tensor.sum(exp_score, 0)

    return alpha


def decoder(cell, inputs, mask, initial_state, attention_states,
            attention_mask, attn_size, dtype=None, scope=None):
    input_size, states_size = cell.input_size
    output_size = cell.output_size
    dtype = dtype or inputs.dtype

    # non sequences should passed to scan, DO NOT use closure
    def loop_fn(inputs, mask, state, attn_states, attn_mask, m_states):
        mask = mask[:, None]
        alpha = attention(state, m_states, output_size, attn_size, attn_mask)
        context = theano.tensor.sum(alpha[:, :, None] * attn_states, 0)
        output, next_state = cell([inputs, context], state)
        next_state = (1.0 - mask) * state +  mask * next_state

        return [next_state, context]

    with ops.variable_scope(scope or "decoder"):
        mapped_states = map_attention_states(attention_states, states_size,
                                             attn_size)
        seq = [inputs, mask]
        outputs_info = [initial_state, None]
        non_seq = [attention_states, attention_mask, mapped_states]
        (states, contexts) = ops.scan(loop_fn, seq, outputs_info, non_seq)

    return states, contexts


class rnnsearch:

    def __init__(self, **option):
        # source and target embedding dim
        sedim, tedim = option["embdim"]
        # source, target and attention hidden dim
        shdim, thdim, ahdim = option["hidden"]
        # maxout hidden dim
        maxdim = option["maxhid"]
        # maxout part
        maxpart = option["maxpart"]
        # deepout hidden dim
        deephid = option["deephid"]
        svocab, tvocab = option["vocabulary"]
        sw2id, sid2w = svocab
        tw2id, tid2w = tvocab
        # source and target vocabulary size
        svsize, tvsize = len(sid2w), len(tid2w)

        if "scope" not in option or option["scope"] is None:
            option["scope"] = "rnnsearch"

        if "initializer" not in option:
            option["initializer"] = None

        if "regularizer" not in option:
            option["regularizer"] = None

        if "keep_prob" not in option:
            option["keep_prob"] = 1.0

        dtype = theano.config.floatX
        scope = option["scope"]
        initializer = option["initializer"]
        regularizer = option["regularizer"]
        keep_prob = option["keep_prob"] or 1.0

        def prediction(prev_inputs, prev_state, context, keep_prob=1.0):
            features = [prev_state, prev_inputs, context]
            maxhid = nn.maxout(features, [[thdim, tedim, 2 * shdim], maxdim],
                               maxpart, True)
            readout = nn.linear(maxhid, [maxdim, deephid], False,
                                scope="deepout")

            if keep_prob < 1.0:
                readout = nn.dropout(readout, keep_prob=keep_prob)

            logits = nn.linear(readout, [deephid, tvsize], True,
                               scope="logits")

            if logits.ndim == 3:
                new_shape = [logits.shape[0] * logits.shape[1], -1]
                logits = logits.reshape(new_shape)

            probs = theano.tensor.nnet.softmax(logits)

            return probs

        # training graph
        with ops.variable_scope(scope, initializer=initializer,
                                regularizer=regularizer, dtype=dtype):
            src_seq = theano.tensor.imatrix("soruce_sequence")
            src_mask = theano.tensor.matrix("soruce_sequence_mask")
            tgt_seq = theano.tensor.imatrix("target_sequence")
            tgt_mask = theano.tensor.matrix("target_sequence_mask")

            with ops.variable_scope("source_embedding"):
                source_embedding = ops.get_variable("embedding",
                                                    [svsize, sedim])
                source_bias = ops.get_variable("bias", [sedim])

            with ops.variable_scope("target_embedding"):
                target_embedding = ops.get_variable("embedding",
                                                [tvsize, tedim])
                target_bias = ops.get_variable("bias", [tedim])

            source_inputs = nn.embedding_lookup(source_embedding, src_seq)
            target_inputs = nn.embedding_lookup(target_embedding, tgt_seq)

            source_inputs = source_inputs + source_bias
            target_inputs = target_inputs + target_bias

            if keep_prob < 1.0:
                source_inputs = nn.dropout(source_inputs, keep_prob=keep_prob)
                target_inputs = nn.dropout(target_inputs, keep_prob=keep_prob)

            cell = nn.rnn_cell.gru_cell([sedim, shdim])

            if keep_prob < 1.0:
                cell = nn.rnn_cell.dropout_wrapper(cell)

            outputs = encoder(cell, source_inputs, src_mask)
            annotation = theano.tensor.concatenate(outputs, 2)

            # compute initial state for decoder
            # first state of backward encoder
            final_state = outputs[1][0]
            with ops.variable_scope("decoder"):
                initial_state = nn.feedforward(final_state, [shdim, thdim],
                                               True, scope="initial",
                                               activation=theano.tensor.tanh)

            cell = nn.rnn_cell.gru_cell([[tedim, 2 * shdim], thdim])

            if keep_prob < 1.0:
                cell = nn.rnn_cell.dropout_wrapper(cell)

            # run decoder
            decoder_outputs = decoder(cell, target_inputs, tgt_mask,
                                      initial_state, annotation, src_mask,
                                      ahdim)
            all_output, all_context = decoder_outputs

            shift_inputs = theano.tensor.zeros_like(target_inputs)
            shift_inputs = theano.tensor.set_subtensor(shift_inputs[1:],
                                                       target_inputs[:-1])

            init_state = initial_state[None, :, :]
            all_states = theano.tensor.concatenate([init_state, all_output], 0)
            prev_states = all_states[:-1]

            with ops.variable_scope("decoder"):
                probs = prediction(shift_inputs, prev_states, all_context,
                                   keep_prob=keep_prob)

            # compute cost
            idx = theano.tensor.arange(tgt_seq.flatten().shape[0])
            cost = -theano.tensor.log(probs[idx, tgt_seq.flatten()])
            cost = cost.reshape(tgt_seq.shape)
            cost = theano.tensor.sum(cost * tgt_mask, 0)
            cost = theano.tensor.mean(cost)

        training_inputs = [src_seq, src_mask, tgt_seq, tgt_mask]
        training_outputs = [cost]

        # decoding graph
        with ops.variable_scope(scope, reuse=True):
            prev_words = theano.tensor.ivector("prev_words")

            # encoder, disable dropout
            source_inputs = nn.embedding_lookup(source_embedding, src_seq)
            source_inputs = source_inputs + source_bias

            cell = nn.rnn_cell.gru_cell([sedim, shdim])
            outputs = encoder(cell, source_inputs, src_mask)
            annotation = theano.tensor.concatenate(outputs, 2)

            # decoder
            final_state = outputs[1][0]
            with ops.variable_scope("decoder"):
                initial_state = nn.feedforward(final_state, [shdim, thdim],
                                               True, scope="initial",
                                               activation=theano.tensor.tanh)

            inputs = nn.embedding_lookup(target_embedding, prev_words)
            inputs = inputs + target_bias

            cond = theano.tensor.neq(prev_words, 0)
            # zeros out embedding if y is 0
            inputs = inputs * cond[:, None]

            cell = nn.rnn_cell.gru_cell([[tedim, 2 * shdim], thdim])

            with ops.variable_scope("decoder"):
                mapped_states = map_attention_states(annotation, 2 * shdim,
                                                     ahdim)
                alpha = attention(initial_state, mapped_states, thdim, ahdim,
                                  src_mask)
                context = theano.tensor.sum(alpha[:, :, None] * annotation, 0)
                output, next_state = cell([inputs, context], initial_state)
                probs = prediction(inputs, initial_state, context)

        # encoding
        encoding_inputs = [src_seq, src_mask]
        encoding_outputs = [annotation, initial_state, mapped_states]
        encode = theano.function(encoding_inputs, encoding_outputs)

        prediction_inputs = [prev_words, initial_state, annotation,
                             mapped_states, src_mask]
        prediction_outputs = [probs, context, alpha]
        predict = theano.function(prediction_inputs, prediction_outputs)

        generation_inputs = [prev_words, initial_state, context]
        generation_outputs = next_state
        generate = theano.function(generation_inputs, generation_outputs)

        self.cost = cost
        self.inputs = training_inputs
        self.outputs = training_outputs
        self.encode = encode
        self.predict = predict
        self.generate = generate
        self.option = option


def beamsearch(model, seq, mask=None, beamsize=10, normalize=False,
               maxlen=None, minlen=None, dtype=None):
    size = beamsize
    dtype = dtype or theano.config.floatX

    # get vocabulary from the first model
    vocab = model.option["vocabulary"][1][1]
    eosid = model.option["eosid"]
    bosid = model.option["bosid"]

    if maxlen == None:
        maxlen = seq.shape[0] * 3

    if minlen == None:
        minlen = seq.shape[0] / 2

    # encoding source
    if mask is None:
        mask = numpy.ones(seq.shape, dtype)

    annotation, states, mapped_annot = model.encode(seq, mask)

    initial_beam = beam(size)
    # bosid must be 0
    initial_beam.candidate = [[bosid]]
    initial_beam.score = numpy.zeros([1], dtype)

    hypo_list = []
    beam_list = [initial_beam]
    cond = lambda x: x[-1] == eosid

    for k in range(maxlen):
        # get previous results
        prev_beam = beam_list[-1]
        candidate = prev_beam.candidate
        num = len(candidate)
        last_words = numpy.array(map(lambda t: t[-1], candidate), "int32")

        # compute context first, then compute word distribution
        batch_mask = numpy.repeat(mask, num, 1)
        batch_annot = numpy.repeat(annotation, num, 1)
        batch_mannot = numpy.repeat(mapped_annot, num, 1)

        outputs = model.predict(last_words, states, batch_annot, batch_mannot,
                                batch_mask)
        prob_dists, contexts, alpha = outputs
        logprobs = numpy.log(prob_dists)

        # do not generate eos symbol
        if k < minlen:
            logprobs[:, eosid] = -numpy.inf

        # force to add eos symbol
        if k == maxlen - 1:
            # copy
            eosprob = logprobs[:, eosid].copy()
            logprobs[:, :] = -numpy.inf
            logprobs[:, eosid] = eosprob

        next_beam = beam(size)
        outputs = next_beam.prune(logprobs, cond, prev_beam)

        # translation complete
        hypo_list.extend(outputs[0])
        batch_indices, word_indices = outputs[1:]
        size -= len(outputs[0])

        if size == 0:
            break

        # generate next state
        candidate = next_beam.candidate
        num = len(candidate)
        last_words = numpy.array(map(lambda t: t[-1], candidate), "int32")

        states = select_nbest(states, batch_indices)
        contexts = select_nbest(contexts, batch_indices)
        states = model.generate(last_words, states, contexts)

        beam_list.append(next_beam)

    # postprocessing
    if len(hypo_list) == 0:
        score_list = [0.0]
        hypo_list = [[eosid]]
    else:
        score_list = [item[1] for item in hypo_list]
        # exclude bos symbol
        hypo_list = [item[0][1:] for item in hypo_list]

    for i, (trans, score) in enumerate(zip(hypo_list, score_list)):
        count = len(trans)
        if count > 0:
            if normalize:
                score_list[i] = score / count
            else:
                score_list[i] = score

    # sort
    hypo_list = numpy.array(hypo_list)[numpy.argsort(score_list)]
    score_list = numpy.array(sorted(score_list))

    output = []

    for trans, score in zip(hypo_list, score_list):
        trans = map(lambda x: vocab[x], trans)
        output.append((trans, score))

    return output
