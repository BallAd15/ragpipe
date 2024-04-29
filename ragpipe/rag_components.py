from typing import List, Optional

from llama_index.core.schema import TextNode
from llama_index.core import load_index_from_storage

from .index import IndexConfig, IndexManager, ObjectIndex, RPIndex, VectorStoreIndexPath


from .common import get_fpath_items, fpath2collection, printd
from .docnode import ScoreNode

from .db import StorageConfig

IM = IndexManager()

from . import llm_bridge
from .encoders import BM25, get_encoder_index_reptype

def encode_and_index(encoder_name, encoder_model, encoder_props, 
                 items, item_paths, repname,
                 storage_config, index_type='rpindex', is_query=False):

    if encoder_name.startswith('llm'):
        prompt = encoder_props.get('prompt', None)
        reps = llm_bridge.transform(items, encoder_name, prompt=prompt, is_query=is_query)
        index_type = 'objindex'

    elif encoder_name == 'passthrough':
        print('computing rep passthrough')
        _repname = repname[1:] # _header -> header
        reps = [item[_repname] for item in items]
        index_type = 'objindex'

    elif encoder_name == 'bm25':
        reps = items if is_query else BM25(items) 
        reps_index = RPIndex(encoder_name='bm25', encoder_model=None, storage_config=storage_config)
        reps_index.add(docs=reps, doc_paths=item_paths, is_query=is_query, docs_already_encoded=True)
        index_type = 'noindex'
    '''
    else:
        if is_query:
            index_type = 'rpindex'
    '''

    match index_type:
        case 'llamaindex':
            assert isinstance(items, list) and len(items) > 0, f"items is {type(items)}"
            ic = IndexConfig(index_type=index_type, encoder_name=encoder_name, 
                doc_paths=item_paths, storage_config=storage_config)
            reps_index = VectorStoreIndexPath.from_docs(items, ic, encoder_model=encoder_model)
            
        case 'rpindex':
            item_type = type(items[0]).__name__
            if item_type != 'str': #handle LI text nodes. TODO: what if LI documents?
                assert 'TextNode' in item_type, f'Cannot handle item type {item_type}'
                items = [item.text for item in items]

            reps_index = RPIndex(encoder_name=encoder_name, encoder_model=encoder_model, 
                                    storage_config=storage_config)
            reps_index.add(docs=items, doc_paths=item_paths, is_query=is_query)

        case 'objindex':
            reps_index = ObjectIndex(reps=reps, paths=item_paths, is_query=is_query, 
                                        docs_already_encoded=True) #

        case 'noindex':
            pass
        case _:
            raise ValueError(f"unknown index: {index_type}")
        
    return reps_index


def compute_rep(fpath, D, props=None, repname=None, is_query=False) -> '*Index':
    #fpath = .sections[].text repname = dense
    assert props is not None
    encoder_name = props.encoder
    storage = props.get('storage', False)
    printd(2, f'props = {props}, storage = {storage}')
  
    ##encoder model loader, index_type, rep_type
    doc_leaf_type = D.get('doc_leaf_type', 'raw')
    ei = get_encoder_index_reptype(encoder_name, doc_leaf_type=doc_leaf_type)  #(TODO: avoid loading model)

    storage_config = None if not storage else StorageConfig(collection_name=fpath2collection(fpath,repname), 
                                                            rep_type=ei.rep_type)
    print(fpath, repname, f': storage={storage_config}, encoder={encoder_name}')

    index_config = IM.get_config(fpath, repname, encoder_name) if storage_config else None
    if index_config is None:

        printd(2, f'Not found in IndexManager cache: creating reps.')

        items_path_pairs = get_fpath_items(fpath, D)
        items, item_paths = items_path_pairs.els, items_path_pairs.paths
        reps_index = encode_and_index(encoder_name, ei.encoder_model_loader(), props, 
                    items, item_paths, repname,    
                    storage_config, index_type=ei.index_type, is_query=is_query)
        if storage_config is not None: #does making a query rpindex make sense? change query?
            IM.add(fpath, repname, encoder_name, reps_index)
        else:
            printd(2, f'storage_config None - not creating index.')

    else:

        printd(2, f'Found in IndexManager cache: {index_config}')
        match index_config.index_type:
            case 'llamaindex':
                reps_index = VectorStoreIndexPath.from_storage(index_config)
            case 'rpindex':
                reps_index = RPIndex.from_index_config(index_config)
            case _ :
                raise ValueError(f'unknown index type {index_config.index_type}')
    #printd(2, f'compute_rep: {fpath} -> {reps_index}')
    return reps_index

def retriever_router(doc_index, query_text, query_index, limit=10): #TODO: rep_query_path -> query_index
    '''
    Depending on the type of index, retrieve the doc nodes relevant to a query
    '''
    from llama_index.core import QueryBundle
    from llama_index.core.retrievers import BaseRetriever, VectorIndexRetriever
    print('doc_index type', type(doc_index), type(query_index))
    #print(doc_index)
    #print(query_index)
    assert(isinstance(query_index, RPIndex)), f'query_index type= {type(query_index)} '
    rep_query = query_index.get_query_rep()

    match doc_index:
        case VectorStoreIndexPath():
            query_bundle = QueryBundle(query_str=query_text, embedding=rep_query)
            retriever = VectorIndexRetriever(index=doc_index.get_vector_store_index(), similarity_top_k=limit)
            li_nodes = retriever.retrieve(query_bundle)
            #vector_ids = {n.node.node_id for n in vector_nodes}
            doc_nodes = [ScoreNode(li_node=n, score=n.score) for n in li_nodes]

            return doc_nodes

        case RPIndex():
            doc_nodes: List[ScoreNode] = doc_index.retrieve(rep_query, limit=limit) #only refs
            return doc_nodes
        
        case _:
            raise NotImplementedError(f'unknown index: {doc_index}')
