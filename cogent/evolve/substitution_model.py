#!/usr/bin/env python
"""
substitution_model.py

Contains classes for defining Markov models of substitution.
These classes depend on an Alphabet class member for defining the set
of motifs that each represent a state in the Markov chain. Examples of
a 'dna' type alphabet motif is 'a', and of a 'codon' type motif is'atg'.

By default all models include the gap motif ('-' for a 'dna' alphabet or
'---' for a 'codon' alphabet). This differs from software such as PAML,
where gaps are treated as ambiguituous states (specifically, as 'n'). The gap
motif state can be excluded from the substitution model using the method
excludeGapMotif(). It is recommended that to ensure the alignment and the
substitution model are defined with the same alphabet that modifications
are done to the substitution model alphabet and this instance is then given
to the alignment.

The model's substitution rate parameters are represented as a dictionary
with the parameter names as keys, and predicate functions as the values.
These predicate functions compare a pair of motifs, returning True or False.
Many such functions are provided as methods of the class. For instance,
the istransition method is pertinent to dna based models. This method returns
True if an 'a'/'g' or 'c'/'t' pair is passed to it, False otherwise. In this
way the positioning of parameters in the instantaneous rate matrix (commonly
called Q) is determined.

>>> model = Nucleotide(equal_motif_probs=True)
>>> model.setparameterrules({'alpha': model.istransition})
>>> parameter_controller = model.makeParamController(tree)
"""

import numpy
from numpy.linalg import svd
import warnings

Float = numpy.core.numerictypes.sctype2char(float)
Int = numpy.core.numerictypes.sctype2char(int)
PyObject = numpy.core.numerictypes.sctype2char(numpy.core.numerictypes.object_)

from cogent.core import moltype
from cogent.evolve import ( substitution_calculation,
    parameter_controller, predicate )
from cogent.evolve.likelihood_tree import makeLikelihoodTreeLeaf

import logging
LOG = logging.getLogger('cogent')

__author__ = "Gavin Huttley and Andrew Butterfield"
__copyright__ = "Copyright 2007-2009, The Cogent Project"
__contributors__ = ["Gavin Huttley", "Andrew Butterfield", "Peter Maxwell",
                    "Matthew Wakefield", "Brett Easton", "Rob Knight",
                    "Von Bing Yap"]
__license__ = "GPL"
__version__ = "1.3.0.dev"
__maintainer__ = "Gavin Huttley"
__email__ = "gavin.huttley@anu.edu.au"
__status__ = "Production"

def predicate2matrix(alphabet, pred, mask=None):
    """From a test like istransition() produce an MxM boolean matrix"""
    M = len(alphabet)
    result = numpy.zeros([M,M], Int)
    for i in range(M):
        for j in range(M):
            if mask is None or mask[i,j]:
                result[i,j] = pred(alphabet[i], alphabet[j])
    return result

def redundancyInPredicateMasks(preds):
    # Calculate the nullity of the predicates.  If non-zero
    # there is some redundancy and the model will be overparameterised.
    masks = preds.values()
    if not masks:
        return 0
    eqns = 1.0 * numpy.array([list(mask.flat) for mask in masks])
    svs = svd(eqns)[1]
    # count non-duplicate non-zeros singular values
    matrix_rank = len([sv for sv in svs if abs(sv) > 1e-8])
    return len(masks) - matrix_rank

def _maxWidthIfTruncated(pars, delim, each):
    # 'pars' is an array of lists of strings, how long would the longest
    # list representation be if the strings were truncated at 'each'
    # characters and joined together with 'delim'.
    return max([
            sum([min(len(par), each) for par in par_list])
            + len(delim) * (len(par_list)-1)
        for par_list in pars.flat])

def _extract_kw(substring, kw):
    """move any keys containg substring into a new dictionary"""
    mkw = {}
    for k in kw.keys():
        if substring in k:
            mkw[k] = kw.pop(k)
    return mkw

def _isSymmetrical(matrix):
    return numpy.alltrue(numpy.alltrue(matrix == numpy.transpose(matrix)))

def _calc_monomer_tuple_indices(tuple_alphabet, monomers):
    """returns arrays for mapping between tuple_alphabet and monomer alphabet"""
    # alphabets should be able to do this.
    # m2w[AC, 1] = C
    # w2m[AA, A] = 2
    size = len(tuple_alphabet)
    length = tuple_alphabet.getMotifLen()
    m2w = numpy.zeros([size, length], Int)
    w2m = numpy.zeros([length, size, len(monomers)], Int)
    for i in range(size):
        for j in range(length):
            monomer = monomers.index(tuple_alphabet[i][j])
            m2w[i, j] = monomer
            w2m[j, i, monomer] = 1
    return (m2w, w2m)

def _calc_monomer_matrix_indices(tuple_alphabet, monomers, mask):
    """return the element indices for flattened matrix for the monomers
    
    Arguments:
        - tuple_alphabet: series of multi-letter motifs
        - monomers: the monomers from which the motifs are made
        - mask: instantaneous change matrix"""
    
    diff_pos = lambda x,y: [i for i in range(len(x)) if x[i] != y[i]]
    mutated_posn = numpy.zeros(mask.shape, Int)
    mutant_motif = numpy.zeros(mask.shape, Int)
    num_states = len(tuple_alphabet)
    for i in range(num_states):
        old_word = tuple_alphabet[i]
        for j in range(num_states):
            new_word = tuple_alphabet[j]
            if mask[i,j]:
                assert mask[i,j] == 1.0
                diffs = diff_pos(old_word, new_word)
                assert len(diffs) == 1, (old_word, new_word)
                mutated_posn[i,j] = diffs[0]
                mutant_motif[i,j] = monomers.index(new_word[diffs[0]])
            else:
                mutated_posn[i,j] = 0  # ignored but must not upset .take()
                mutant_motif[i,j] = 0  # values here ignored, mask used.
    return (mutated_posn, mutant_motif, mask)

class _SubstitutionModel(object):
    # Subclasses must provide
    #  .makeParamControllerDefns()
    #  .getAlphabet()
    
    def __str__(self):
        s = ["\n%s (" % self.__class__.__name__ ]
        s.append("name = '%s'; type = '%s';" %
                (getattr(self, "name", None), getattr(self, "type", None)))
        if hasattr(self, "predicate_masks"):
            parlist = self.predicate_masks.keys()
            s.append("params = %s;" % parlist)
        motifs = self.getMotifs()
        s.append("number of motifs = %s;" % len(motifs))
        s.append("motifs = %s)\n" % motifs)
        return " ".join(s)
    
    def getMotifs(self):
        return list(self.getAlphabet())
    
    def makeLikelihoodFunction(self, tree, motif_probs_from_align=None,
            optimise_motif_probs=None, aligned=True, expm='either', **kw):
        
        if motif_probs_from_align is None:
            motif_probs_from_align = self.motif_probs_from_align
        
        if optimise_motif_probs is None:
            optimise_motif_probs = self._optimise_motif_probs
        
        kw['optimise_motif_probs'] = optimise_motif_probs
        kw['motif_probs_from_align'] = motif_probs_from_align
        
        if aligned:
            klass = parameter_controller.AlignmentLikelihoodFunction
        else:
            alphabet = self.getAlphabet()
            assert alphabet.getGapMotif() not in alphabet
            klass = parameter_controller.SequenceLikelihoodFunction
        
        result = klass(self, tree, **kw)
        
        if self.motif_probs is not None:
            result.setMotifProbs(self.motif_probs, is_const=
                not optimise_motif_probs, auto=True)
        
        result.setExpm(expm)
        
        return result
    
    def makeParamController(self, tree, motif_probs_from_align=None,
            optimise_motif_probs=None, **kw):
        # deprecate
        return self.makeLikelihoodFunction(tree,
                motif_probs_from_align = motif_probs_from_align,
                optimise_motif_probs = optimise_motif_probs,
                **kw)
    
    def convertAlignment(self, alignment):
        # this is to support for everything but HMM
        result = {}
        for seq_name in alignment.getSeqNames():
            sequence = alignment.getGappedSeq(seq_name, self.recode_gaps)
            result[seq_name] = self.convertSequence(sequence, seq_name)
        return result
    
    def convertSequence(self, sequence, name):
        # makeLikelihoodTreeLeaf, sort of an indexed profile where duplicate
        # columns stored once, so likelihoods only calc'd once
        return makeLikelihoodTreeLeaf(sequence, self.getAlphabet(), name)
    
    def countMotifs(self, alignment, include_ambiguity=False):
        alphabet = [self.getMprobAlphabet(), self.getAlphabet()][
                self.position_specific_mprobs]
        result = None
        for seq_name in alignment.getSeqNames():
            sequence = alignment.getGappedSeq(seq_name, self.recode_gaps)
            leaf = makeLikelihoodTreeLeaf(sequence, alphabet, 
                    seq_name)
            count = leaf.getMotifCounts(include_ambiguity=include_ambiguity)
            if result is None:
                result = count.copy()
            else:
                result += count
        return result

class SubstitutionModel(_SubstitutionModel):
    """Basic services for markov models of molecular substitution"""
    
    def __init__(self, alphabet, predicates=None, scales=None,
            motif_probs=None, optimise_motif_probs=False,
            equal_motif_probs=False, motif_probs_from_data=None,
            motif_probs_alignment=None, mprob_model=None,
            rate_matrix=None, word_length=None,
            model_gaps=False, recode_gaps=False, motif_length=None,
            do_scaling=True, with_rate=False, name="", motifs=None,
            ordered_param=None, distribution=None, partitioned_params=None,
            ):
        
        """Initialise the model.
        
        Arguments:
            - alphabet: an alphabet object
            - predicates: a dict of {name:(motif,motif)->bool}
            - optimise_motif_probs: flag for whether the motifs are treated as
              free parameters for an optimisation, default is False.
            - motif_probs: dictionary of probabilities, or None if they are to
              be calculated from the alignment. If optimise_motif_probs is set
              these will only be used as initial values.
            - mprob_model: If 'monomer', the model Alphabet monomer
              motif probabilities will be computed from motif probabilities.
              Rate matrix elements then include the probability of the monomer
              end state, eg an interchange between dinucleotide ij <=> ik
              will be scaled by the probability P(k), not P(ik).
              If 'monomers' position specific mprobs will be used.
            - model_gaps: specifies whether the gap motif should be included
              as a state in the Markov chain.
            - recode_gaps: specifies whether gaps in an alignment should be
              treated as an ambiguous state instead.
            - do_scaling: automatically scale branch lengths as the expected
              number of substitutions, default is True.
            """
        
        # - with_rate: pertinent only for binned lengths
        # - scales: scale rules, dict with predicates
        # - motif_probs_alignment: motif probs from full alignment, see
        #   Vestige
        # - motifs: make a subalphabet that only contains those motifs
        # - ordered_param: a single parameter name (str) or a series of
        #   parameter names
        # - distribution: choices of 'free' or 'gamma' or an instance of some
        #   distribution. Could probably just deprecate free
        # - rate_matrix: for empirical matrices
        # - partitioned_params: params to be partitioned across bins
        
        # the following is a hack to ensure the interface of the version of
        # cogent published with the Lindsay et al Biology Direct manuscript
        # has the same interface as that which will finally be used.
        use_monomer_probs = None
        position_specific_mprobs = False
        if mprob_model == 'monomer':
            use_monomer_probs = True
        elif mprob_model == 'monomers':
            use_monomer_probs = True
            position_specific_mprobs = True
        
        # MISC
        assert len(alphabet) < 65, "Alphabet too big. Try explicitly "\
            "setting alphabet to PROTEIN or DNA"
        
        self.name = name
        self._optimise_motif_probs = optimise_motif_probs
        self._canned_predicates = None
        self._do_scaling = do_scaling
        if self._do_scaling:
            self.rateMatrixClass = substitution_calculation.ScaledRateMatrix
        else:
            self.rateMatrixClass = substitution_calculation.RateMatrix
        
        # ALPHABET
        
        if recode_gaps:
            if model_gaps:
                LOG.warning("Converting gaps to wildcards AND modeling gaps")
            else:
                model_gaps = False
        
        self.recode_gaps = recode_gaps
        
        self.MolType = alphabet.MolType
        if model_gaps:
            alphabet = alphabet.withGapMotif()
                
        # The tidy way to use new-Q is to derive the word-alphabet from the
        # monomer-alphabet via 'word_length'
        # The transitional way to use new-Q is to accept or make the 
        # word-alphabet then extract the monomer-alphabet from it.
        if word_length > 1:
            assert motif_length is None, 'word_length OR motif_length'
            if use_monomer_probs is None:
                use_monomer_probs = True
            motif_alphabet = alphabet
            alphabet = motif_alphabet.getWordAlphabet(word_length)
            if not use_monomer_probs:
                motif_alphabet = alphabet
        else:
            if motif_length > 1:
                alphabet = alphabet.getWordAlphabet(motif_length)
            if use_monomer_probs:
                # interface deprecation warning?
                motif_alphabet = alphabet.MolType.Alphabet
            else:
                motif_alphabet = alphabet
            word_length = alphabet.getMotifLen() // motif_alphabet.getMotifLen()

        if motifs is not None:
            alphabet = alphabet.getSubset(motifs)
        self.alphabet = alphabet
        self.gapmotif = alphabet.getGapMotif()
        self._mprobs_alphabet = motif_alphabet
        self._word_length = word_length
        
        # MATRIX
        # truth (_instantaneous_mask) mask may not be needed
        if rate_matrix is not None:
            assert rate_matrix.shape == (len(self.alphabet),)*2
            assert numpy.alltrue(numpy.diagonal(rate_matrix) == 0)
            self._instantaneous_mask_f = rate_matrix * 1.0
            self._instantaneous_mask = (self._instantaneous_mask_f != 0.0)
            if predicates:
                warnings.warn('Empirical model with parameters too!')
        else:
            isinst = self._isInstantaneous
            self._instantaneous_mask = predicate2matrix(self.alphabet, isinst)
            self._instantaneous_mask_f = self._instantaneous_mask * 1.0
        
        self.symmetric = _isSymmetrical(self._instantaneous_mask_f)
        predicate_masks = self._adaptPredicates(predicates or [])
        self.checkPredicateMasks(predicate_masks)
        self.predicate_masks = predicate_masks
        self.parameter_order = []
        self.predicate_indices = []
        for (pred, mask) in predicate_masks.items():
            if not _isSymmetrical(mask):
                self.symmetric = False
            indices = numpy.nonzero(mask.ravel())[0]
            assert numpy.alltrue(numpy.take(mask.flat, indices, 0) == 1)
            self.parameter_order.append(pred)
            self.predicate_indices.append(indices)
        if not self.symmetric:
            warnings.warn('Model not reversible')
        
        self.scale_masks = self._adaptPredicates(scales or [])
        
        # MOTIF PROB ALPHABET MAPPING
        if use_monomer_probs:
            if model_gaps:
                raise ValueError("Gapped new-Q context models not yet possible")
            self._monomer_indices = _calc_monomer_tuple_indices(
                    alphabet, motif_alphabet)
            self._monomer_matrix_indices = _calc_monomer_matrix_indices(
                    alphabet, motif_alphabet, self._instantaneous_mask_f)
        else:
            self._monomer_indices = None
            self._monomer_matrix_indices = None
        
        # MOTIF PROBS
        self.position_specific_mprobs = position_specific_mprobs
        
        if equal_motif_probs:
            assert not (motif_probs or motif_probs_alignment), \
                    "Motif probs equal or provided but not both"
            motif_probs = {}
            for motif in self._mprobs_alphabet:
                motif_probs[motif] = 1.0 / len(self._mprobs_alphabet)
        elif motif_probs_alignment is not None:
            assert not motif_probs, \
                    "Motif probs from alignment or provided but not both"
            motif_probs = self.countMotifs(motif_probs_alignment)
            motif_probs = motif_probs.astype(Float) / sum(motif_probs)
            motif_probs = dict(zip(self._mprobs_alphabet, motif_probs))
        if motif_probs:
            self.adaptMotifProbs(motif_probs) # to check
            self.motif_probs = motif_probs
            if motif_probs_from_data is None:
                motif_probs_from_data = False
        else:
            self.motif_probs = None
            if motif_probs_from_data is None:
                motif_probs_from_data = True
        self.motif_probs_from_align = motif_probs_from_data

        # BINS
        if isinstance(ordered_param, str):
            ordered_param = (ordered_param,)
        else:
            ordered_param = [(), ordered_param][ordered_param is not None]
            ordered_param = tuple(ordered_param)
        
        if isinstance(partitioned_params, str):
            partitioned_params = (partitioned_params,)
        else:
            partitioned_params = [(), partitioned_params][partitioned_params is not None]
        
        if ordered_param:
            partitioned_params = tuple(set(ordered_param) | \
                                       set(partitioned_params))
        # for a bin model, one param needs to be defined as the ordered_param
        else:
            assert not partitioned_params, \
                "you must specify an ordered_param for a binned model"
        
        self.with_rate = with_rate or 'rate' in ordered_param
        self.ordered_param = ordered_param
        
        if partitioned_params:
            assert set(partitioned_params) & set(['rate']+self.parameter_order),\
                (partitioned_params, self.parameter_order)
        self.partitioned_params = partitioned_params
        
        if distribution == "gamma":
            distribution = substitution_calculation.GammaDefn
        elif distribution in [None, "free"]:
            distribution = substitution_calculation.MonotonicDefn
        elif isinstance(distribution, basestring):
            raise ValueError('Unknown distribution "%s"' % distribution)
        self.distrib_class = distribution
        
        # CACHED SHORTCUTS
        self._exponentiator = None
        self._ident = numpy.identity(len(self.alphabet), Float)
    
    def getAlphabet(self):
        return self.alphabet
    
    def getMprobAlphabet(self):
        return self._mprobs_alphabet
        
    def calcRateMatrix(self, *params):
        assert len(params) == len(self.predicate_indices), self.parameter_order
        inst = self._instantaneous_mask_f
        work = numpy.ones(inst.shape, Float)
        F = inst.copy()
        for (indices, par) in zip(self.predicate_indices, params):
            numpy.put(work, indices, par)
            F *= work
            work[:] = 1.0
            
        return self.rateMatrixClass(F, self._ident, self.symmetric)
    
    def suitableEigenExponentiators(self):
        # Uses a fake Q to compare the eigenvalue implementations
        # with.  This assumes that one Q will be much like another.
        import random
        if self._exponentiator is None:
            params = [random.uniform(0.5, 2.0) for p in self.parameter_order]
            R = self.calcRateMatrix(*params)
            if self.motif_probs:
                monomer_probs =self.adaptMotifProbs(self.motif_probs, auto=True)
            else:
                monomer_probs = numpy.array(
                    [random.uniform(0.2, 1.0) for m in self.getMprobAlphabet()])
            monomer_probs /= sum(monomer_probs)
            word_probs = self.calcWordProbs(monomer_probs)
            mprobs_matrix = self.calcWordWeightMatrix(monomer_probs)
            self._exponentiator = R.getFastEigenExponentiators(
                word_probs, mprobs_matrix)
        return self._exponentiator
    
    # At some point this can be made variable, and probably
    # the default changed to False
    long_indels_are_instantaneous = True
    
    def _isInstantaneous(self, x, y):
        diffs = sum([X!=Y for (X,Y) in zip(x,y)])
        return diffs == 1 or (diffs > 1 and
                self.long_indels_are_instantaneous and self._isAnyIndel(x, y))
    
    def _isAnyIndel(self, x, y):
        """An indel of any length"""
        # Things get complicated when a contigous indel of any length is OK:
        if x == y:
            return False
        gap_start = gap_end = gap_strand = None
        for (i, (X,Y)) in enumerate(zip(x,y)):
            G = self.gapmotif[i]
            if X != Y:
                if X != G and Y != G:
                    return False  # non-gap differences had their chance above
                elif gap_start is None:
                    gap_start = i
                    gap_strand = [X,Y].index(G)
                elif gap_end is not None or [X,Y].index(G) != gap_strand:
                    return False # can't start a second gap
                else:
                    pass # extend open gap
            elif gap_start is not None:
                gap_end = i
        return True
    
    def asciiArt(self, delim='', delim2='|', max_width=70):
        """An ASCII-art table representing the model.  'delim' delimits
        parameter names, 'delim2' delimits motifs"""
        # Should be implemented with table module instead.
        
        pars = self.getMatrixParams()
        par_names = self.getParamList()
        longest = max([len(name) for name in (par_names+[' '])])
        if delim:
            all_names_len = _maxWidthIfTruncated(pars, delim, 100)
            min_names_len = _maxWidthIfTruncated(pars, delim, 1)
        else:
            all_names_len = sum([len(name) for name in par_names])
            min_names_len = len(par_names)
        
        # Find a width-per-motif that is as big as can be without being too big
        w = min_names_len
        while (w+1) * len(self.alphabet) < max_width and w < all_names_len:
            w += 1
        
        # If not enough width truncate parameter names
        if w < all_names_len:
            each = w / len(par_names)
            if delim:
                while _maxWidthIfTruncated(pars, delim, each+1) <= w:
                    each += 1
                w = _maxWidthIfTruncated(pars, delim, each)
            else:
                w = each * len(par_names)
        else:
            each = longest
        
        rows = []
        # Only show header if there is enough width for the motifs
        if self.alphabet.getMotifLen() <= w:
            header = [str(motif).center(w) for motif in self.alphabet]
            header = [' ' * self.alphabet.getMotifLen() + ' '] + header + ['']
            header = delim2.join(header)
            rows.append(header)
            rows.append(''.join([['-',delim2][c == delim2] for c in header]))
        
        # pars in sub-cols, should also offer pars in sub-rows?
        for (motif, row2) in zip(self.alphabet, pars):
            row = []
            for par_list in row2:
                elt = []
                for par in par_names:
                    if par not in par_list:
                        par = ''
                    par = par[:each]
                    if not delim:
                        par = par.ljust(each)
                    if par:
                        elt.append(par)
                elt = delim.join(elt).ljust(w)
                row.append(elt)
            rows.append(delim2.join(([motif+' '] + row + [''])))
        return '\n'.join(rows)
    
    def getMatrixParams(self):
        """Return the parameter assignment matrix."""
        dim = len(self.alphabet)
        Pars = numpy.zeros([dim, dim], PyObject)
        for x, y in [(x, y) for x in range(dim) for y in range(dim)]:
            Pars[x][y] = []  # a limitation of numpy.  [x,y] = [] fails!
            if not self._instantaneous_mask[x, y]:
                continue
            for par in self.predicate_masks:
                if self.predicate_masks[par][x, y]:
                    Pars[x, y].append(par)
            # sort the matrix entry to facilitate scaling calculations
            Pars[x, y].sort()
        return Pars
    
    def getWordLength(self):
        return self._word_length
        
    def getMotifProbs(self):
        """Return the dictionary of motif probabilities."""
        return self.motif_probs.copy()
    
    def getParamList(self):
        """Return a list of parameter names."""
        return self.predicate_masks.keys()
    
    def isInstantaneous(self, x, y):
        return self._isInstantaneous(x, y)
    
    def getSubstitutionRateValueFromQ(self, Q, motif_probs, pred):
        pred_mask = self._adaptPredicates([pred]).values()[0]
        pred_row_totals = numpy.sum(pred_mask * Q, axis=1)
        inst_row_totals = numpy.sum(self._instantaneous_mask * Q, axis=1)
        r = sum(pred_row_totals * motif_probs)
        t = sum(inst_row_totals * motif_probs)
        pred_size = numpy.sum(pred_mask.flat)
        inst_size = sum(self._instantaneous_mask.flat)
        return (r / pred_size) / ((t-r) / (inst_size-pred_size))
    
    def getScaledLengthsFromQ(self, Q, motif_probs, length):
        lengths = {}
        for rule in self.scale_masks:
            lengths[rule] = length * self.getScaleFromQs(
                    [Q], [1.0], motif_probs, rule)
        return lengths
    
    def getScaleFromQs(self, Qs, bin_probs, motif_probss, rule):
        rule = self.getPredicateMask(rule)
        weighted_scale = 0.0
        bin_probs = numpy.asarray(bin_probs)
        for (Q, bin_prob, motif_probs) in zip(Qs, bin_probs, motif_probss):
            row_totals = numpy.sum(rule * Q, axis=1)
            motif_probs = numpy.asarray(motif_probs)
            word_probs = self.calcWordProbs(motif_probs)
            scale = sum(row_totals * word_probs)
            weighted_scale += bin_prob * scale
        return weighted_scale
    
    def getPredefinedPredicates(self):
        # overridden in subclasses
        return {'indel': predicate.parse('-/?')}
    
    def getPredefinedPredicate(self, name):
        # Called by predicate parsing code
        if self._canned_predicates is None:
            self._canned_predicates = self.getPredefinedPredicates()
        return self._canned_predicates[name].interpret(self)
    
    def checkPredicateMasks(self, predicate_masks):
        # Check for redundancy in predicates, ie: 1 or more than combine
        # to be equivalent to 1 or more others, or the distance params.
        # Give a clearer error in simple cases like always false or true.
        for (name, matrix) in predicate_masks.items():
            if numpy.alltrue((matrix == 0).flat):
                raise ValueError("Predicate %s is always false." % name)
        predicates_plus_scale = predicate_masks.copy()
        predicates_plus_scale[None] = self._instantaneous_mask
        if self._do_scaling:
            for (name, matrix) in predicate_masks.items():
                if numpy.alltrue((matrix == self._instantaneous_mask).flat):
                    raise ValueError("Predicate %s is always true." % name)
            if redundancyInPredicateMasks(predicate_masks):
                raise ValueError("Redundancy in predicates.")
            if redundancyInPredicateMasks(predicates_plus_scale):
                raise ValueError("Some combination of predicates is"
                        " equivalent to the overall rate parameter.")
        else:
            if redundancyInPredicateMasks(predicate_masks):
                raise ValueError("Redundancy in predicates.")
            if redundancyInPredicateMasks(predicates_plus_scale):
                LOG.warning("do_scaling=True would be more efficient than"
                        " these overly general predicates")
    
    def _adaptPredicates(self, rules):
        # dict or list of callables, predicate objects or predicate strings
        if isinstance(rules, dict):
            rules = rules.items()
        else:
            rules = [(None, rule) for rule in rules]
        predicate_masks = {}
        for (key, pred) in rules:
            (label, mask) = self.adaptPredicate(pred, key)
            if label in predicate_masks:
                raise KeyError('Duplicate predicate name "%s"' % label)
            predicate_masks[label] = mask
        return predicate_masks
    
    def adaptPredicate(self, pred, label=None):
        if isinstance(pred, str):
            pred = predicate.parse(pred)
        elif callable(pred):
            pred = predicate.UserPredicate(pred)
        pred_func = pred.makeModelPredicate(self)
        label = label or repr(pred)
        mask = predicate2matrix(
            self.getAlphabet(), pred_func, mask=self._instantaneous_mask)
        return (label, mask)
    
    def getPredicateMask(self, pred):
        if pred in self.scale_masks:
            mask = self.scale_masks[pred]
        elif pred in self.predicate_masks:
            mask = self.predicate_masks[pred]
        else:
            (label, mask) = self.adaptPredicate(pred)
        return mask
    
    def makeAlignmentDefn(self, model):
        align = substitution_calculation.NonParamDefn(
                'alignment', ('locus',))
        # The name of this matters, it's used in likelihood_function.py
        # to retrieve the correct (adapted) alignment.
        return substitution_calculation.AlignmentAdaptDefn(
            model, align)
    
    def makeMotifProbsDefn(self):
        """Makes the first part of a parameter controller definition for this
        model, the calculation of motif probabilities"""
        if self.position_specific_mprobs:
            dimensions = ('locus', 'position')
        else:
            dimensions = ('locus',)
        return substitution_calculation.PartitionDefn(
                name="mprobs", default=None, dimensions=dimensions,
                dimension=('motif', tuple(self.getMprobAlphabet())))

    def makeMotifPosnProbsDefns(self, monomer_probs):
        if self.position_specific_mprobs:
            return [substitution_calculation.SelectFromDimension(
                monomer_probs, position=str(i)) for i in 
                range(self._word_length)]
        else:
            return [monomer_probs]

    def adaptMotifProbs(self, motif_probs, auto=False):
        alphabet = self.getMprobAlphabet()
        recode_mprobs = False
        if hasattr(motif_probs, 'keys'):
            sample = motif_probs.keys()[0]
            if sample not in alphabet:
                alphabet = self.getAlphabet()
                recode_mprobs = True
                if sample not in alphabet:
                        raise ValueError("Can't find motif %s in alphabet" %
                                sample)
            motif_probs = numpy.array(
                    [motif_probs.get(motif, 0) for motif in alphabet])
        else:
            if len(motif_probs) != len(alphabet):
                alphabet = self.getAlphabet()
                recode_mprobs = True
                if len(motif_probs) != len(alphabet):
                    raise ValueError("Can't match %s probs to %s alphabet" %
                            (len(motif_probs), len(alphabet)))
            motif_probs = numpy.asarray(motif_probs)
        assert abs(sum(motif_probs)-1.0) < 0.0001, motif_probs
        if recode_mprobs:
            motif_probs = self.calcMonomerProbs(motif_probs)
            if not auto and not self.position_specific_mprobs:
                warnings.warn('Motif probs overspecified', stacklevel=4)
        elif self.position_specific_mprobs:
            motif_probs = [motif_probs.copy() for i in range(self._word_length)]
        return motif_probs

    def calcMonomerProbs(self, word_probs):
        # Not presently used, always go monomer->word instead
        if self._monomer_indices is None:
            return word_probs
        (m2w, w2m) = self._monomer_indices
        if not self.position_specific_mprobs:
            monomer_probs = numpy.dot(word_probs, w2m.sum(axis=0))
            monomer_probs /= monomer_probs.sum()
        else:
            monomer_probs = numpy.dot(word_probs, w2m)
            monomer_probs /= monomer_probs.sum(axis=1)[..., numpy.newaxis]
            monomer_probs = list(monomer_probs)
        return monomer_probs
    
    def calcWordProbs(self, *monomer_probs):  
        if self._monomer_indices is None:
            assert len(monomer_probs) == 1
            return monomer_probs[0]
        (m2w, w2m) = self._monomer_indices
        if len(monomer_probs) == 1:
            result = numpy.product(monomer_probs[0].take(m2w), axis=-1)
            # maybe simpler but slower, works ok:
            #result = numpy.product(monomer_probs[0] ** (w2m, axis=-1))
        else:
            assert len(monomer_probs) == m2w.shape[1]
            result = numpy.product(
                [monomer_probs[i].take(m2w[:,i]) 
                for i in range(len(monomer_probs))], axis=0)
        result /= result.sum()
        return result
    
    def calcWordWeightMatrix(self, *monomer_probs):  
        if self._monomer_matrix_indices is None:
            assert len(monomer_probs) == 1
            return monomer_probs[0]
        (positions, indices, mask) = self._monomer_matrix_indices
        if len(monomer_probs) == 1:
            result = monomer_probs[0].take(indices) * mask
        else:
            monomer_probs = numpy.array(monomer_probs) # so [posn, motif]
            size = monomer_probs.shape[-1]
            extended_indices = positions * size + indices # should be constant
            result = monomer_probs.take(extended_indices) * mask
        return result
    
    def makeQdDefn(self, word_probs, mprobs_matrix, rate_params):
        NonParamDefn = substitution_calculation.NonParamDefn
        expm = NonParamDefn('expm')
        exp = substitution_calculation.ExpDefn(expm, model=self)
        
        return substitution_calculation.QdDefn(
            exp,
            word_probs,
            mprobs_matrix,
            *rate_params,
            **dict(calc_rate_matrix = self.calcRateMatrix)
            )
    
    def makePsubsDefn(self, Qd, distance):
        """Makes the second part of the parameter controller definition,
        psubs given motif probs and lengths"""
        return substitution_calculation.CallDefn(Qd, distance, name='psubs')
    
    def _makeBinParamDefn(self, edge_par_name, bin_par_name, bprob_defn):
        # if no ordered param defined, behaves as old, everything indexed by and edge
        SelectForDimension = substitution_calculation.SelectForDimension
        WeightedPartitionDefn = substitution_calculation.WeightedPartitionDefn
        ParamDefn = substitution_calculation.SubstitutionParameterDefn
        if edge_par_name not in self.partitioned_params:
            return ParamDefn(dimensions=['bin'], name=bin_par_name)
        
        if edge_par_name in self.ordered_param:
            whole = self.distrib_class(bprob_defn, bin_par_name)
        else:
            # this forces them to average to one, but no forced order
            # this means you can't force a param value to be shared across bins
            # so 1st above approach has to be used
            whole = WeightedPartitionDefn(bprob_defn, bin_par_name+'_partn')
        whole.bin_names = bprob_defn.bin_names
        return SelectForDimension(whole, 'bin', name=bin_par_name)
    
    def makeFundamentalParamControllerDefns(self, bin_names):
        ParamDefn = substitution_calculation.SubstitutionParameterDefn
        RateDefn = substitution_calculation.RateDefn
        LengthDefn = substitution_calculation.LengthDefn
        ProductDefn = substitution_calculation.ProductDefn
        
        if len(bin_names) > 1:
            bprobs = substitution_calculation.PartitionDefn(
                [1.0/len(bin_names) for bin in bin_names], name = "bprobs",
                dimensions=['locus'], dimension=('bin', bin_names))
        else:
            bprobs = None
        
        length = substitution_calculation.LengthDefn()
        if self.with_rate and bprobs is not None:
            b_rate = self._makeBinParamDefn('rate', 'rate', bprobs)
            distance = substitution_calculation.ProductDefn(length, b_rate,
                name="distance")
        else:
            distance = length
        
        model = substitution_calculation.ConstDefn(self, 'model')
        rate_params = []
        for param_name in self.parameter_order:
            if param_name not in self.partitioned_params:
                defn = ParamDefn(name=param_name)
            else:
                defn = ParamDefn(param_name, dimensions=['edge', 'locus'])
                if bprobs is not None:
                    # should be weighted by bprobs*rates not bprobs
                    b_defn = self._makeBinParamDefn(
                            param_name, param_name+'_factor', bprobs)
                    defn = ProductDefn(b_defn, defn, name=param_name+'_BE')
            rate_params.append(defn)
            
        monomer_probs = self.makeMotifProbsDefn()
        monomer_probs3 = self.makeMotifPosnProbsDefns(monomer_probs)
        word_probs = mprobs_matrix = monomer_probs
        if self._monomer_indices is not None:
            word_probs = substitution_calculation.CalcDefn(
                self.calcWordProbs, name="wprobs")(*monomer_probs3)
        if self._monomer_matrix_indices is not None:
            mprobs_matrix = substitution_calculation.CalcDefn(
                self.calcWordWeightMatrix, name="mprobs_matrix")(
                    *monomer_probs3)
        Qd = self.makeQdDefn(word_probs, mprobs_matrix, rate_params)
        
        defns = {
            'model': model,
            'motif_probs': monomer_probs,  
            'word_probs': word_probs,
            'mprobs_matrix': mprobs_matrix,
            'length': length,
            'distance': distance,
            'Qd': Qd,
            'bprobs': bprobs,
            }
        return defns
    
    def makeParamControllerDefns(self, bin_names):
        defns = self.makeFundamentalParamControllerDefns(bin_names)
        defns.update({
            'align': self.makeAlignmentDefn(defns['model']),
            'psubs': self.makePsubsDefn(defns['Qd'], defns['distance']),
            })
        return defns
    

class _Nucleotide(SubstitutionModel):
    def getPredefinedPredicates(self):
        return {
            'transition' : predicate.parse('R/R') | predicate.parse('Y/Y'),
            'transversion' : predicate.parse('R/Y'),
            'indel': predicate.parse('-/?'),
            }
    

class Nucleotide(_Nucleotide):
    """A nucleotide substitution model."""
    def __init__(self, **kw):
        SubstitutionModel.__init__(self, moltype.DNA.Alphabet, **kw)
    

class Dinucleotide(_Nucleotide):
    """A nucleotide substitution model."""
    def __init__(self, **kw):
        SubstitutionModel.__init__(self, moltype.DNA.Alphabet, motif_length=2, **kw)
    

class Protein(SubstitutionModel):
    """Base protein substitution model."""
    def __init__(self, with_selenocysteine=False, **kw):
        alph = moltype.PROTEIN.Alphabet
        if not with_selenocysteine:
            alph = alph.getSubset('U', excluded=True)
        SubstitutionModel.__init__(self, alph, **kw)
    

def EmpiricalProteinMatrix(matrix, motif_probs=None, optimise_motif_probs=False,
        recode_gaps=True, do_scaling=True, name=""):
    return Protein(rate_matrix=matrix, motif_probs=motif_probs,
            model_gaps=False, recode_gaps=recode_gaps, do_scaling=do_scaling,
            optimise_motif_probs=optimise_motif_probs, name=name)
    

class Codon(_Nucleotide):
    """Core substitution model for codons"""
    long_indels_are_instantaneous = True
    
    def __init__(self, alphabet=None, gc=None, **kw):
        if gc is not None:
            alphabet = moltype.CodonAlphabet(gc = gc)
        alphabet = alphabet or moltype.STANDARD_CODON
        SubstitutionModel.__init__(self, alphabet, **kw)
    
    def _isInstantaneous(self, x, y):
        if x == self.gapmotif or y == self.gapmotif:
            return x != y
        else:
            ndiffs = sum([X!=Y for (X,Y) in zip(x,y)])
            return ndiffs == 1
    
    def getPredefinedPredicates(self):
        gc = self.getAlphabet().getGeneticCode()
        def silent(x, y):
            return x != '---' and y != '---' and gc[x] == gc[y]
        def replacement(x, y):
            return x != '---' and y != '---' and gc[x] != gc[y]
        
        preds = _Nucleotide.getPredefinedPredicates(self)
        preds.update({
            'indel' : predicate.parse('???/---'),
            'silent' : predicate.UserPredicate(silent),
            'replacement' : predicate.UserPredicate(replacement),
            })
        return preds
    
