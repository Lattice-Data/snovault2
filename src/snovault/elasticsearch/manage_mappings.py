import json

import os

from pathlib import Path

from pyramid.paster import get_app

from pyramid.router import Router

from webtest import TestApp

from opensearchpy import OpenSearch

from opensearchpy.exceptions import NotFoundError

from dataclasses import dataclass

from .create_mapping import generate_indices_and_mappings
from .create_mapping import index_settings as get_index_settings
from .create_mapping import create_and_set_index_mapping

from .interfaces import ELASTIC_SEARCH
from .interfaces import RESOURCES_INDEX as ALL_RESOURCES_ALIAS

from typing import Any
from typing import Dict
from typing import Literal


@dataclass
class ManageMappingsProps:
    app: Router
    opensearch_client: OpenSearch
    type_alias_to_current_index_name: Dict[str, Any]
    mappings: Dict[str, Any]
    all_resources_alias: str
    should_reindex: Literal['always', 'never', 'after-initial']
    summary: Dict[str, Any]


def reindex_by_collection(app, collection):
    environ = {
        'HTTP_ACCEPT': 'application/json',
        'REMOTE_USER': 'INDEXER',
    }
    testapp = TestApp(
        app,
        environ
    )
    response = testapp.post_json(
        f'/_invalidate_by_collection?collection={collection}',
        {},
    )
    print(response.json)


def get_aliases(props: ManageMappingsProps, type_alias):
    return {
        type_alias: {},
        props.all_resources_alias: {},
    }


def create_index(props: ManageMappingsProps, type_alias, current_index_name):
    print(f'Creating index {current_index_name} for type {type_alias}')
    index_settings = get_index_settings()
    index_settings['aliases'] = get_aliases(
        props,
        type_alias
    )
    create_and_set_index_mapping(
        es=props.opensearch_client,
        index=current_index_name,
        index_settings=index_settings,
        mapping=props.mappings[type_alias],
    )
    props.summary['created'].append(
        current_index_name
    )


def reindex_collections(props, collections):
    for collection in collections:
        print('Reindexing', collection)
        reindex_by_collection(
            props.app,
            collection,
        )
        props.summary['reindexed'].append(
            collection
        )


def clean_up_auto_created_indices(props: ManageMappingsProps):
    # We can't turn off auto_create_index at cluster level so we'll clean up manually
    # if any of our workers start writing to an index that doesn't exist yet.
    current_index_names = get_current_index_names(
        props.type_alias_to_current_index_name
    )
    try:
        all_existing_resources_indices = list(
            props.opensearch_client.indices.get_alias(
                props.all_resources_alias
            ).keys()
        )
    except NotFoundError as e:
        print(e)
        # Give up if resources alias doesn't exist yet.
        return
    for current_index_name in current_index_names:
        if current_index_name in all_existing_resources_indices:
            # It's aliased so we created it.
            continue
        if props.opensearch_client.indices.exists(current_index_name):
            # It exists but we didn't create it.
            print('Deleting unaliased (autocreated?) index', current_index_name)
            print(props.opensearch_client.indices.delete(current_index_name))
            props.summary['cleaned_up'].append(
                current_index_name
            )


def resources_alias_exists(props: ManageMappingsProps):
    return bool(
        props.opensearch_client.indices.exists_alias(
            name=props.all_resources_alias,
        )
    )


def should_reindex_collection(props: ManageMappingsProps, resources_alias_existed):
    if props.should_reindex == 'never':
        return False
    elif props.should_reindex == 'always':
        return True
    elif props.should_reindex == 'after-initial':
        # Reindex unless this run is the one creating the cluster's first
        # indices. This used to look for the type's own alias instead, which
        # breaks for a type whose index delete_old_indices removed earlier
        # (gone in one release or branch, back in the next): its alias is
        # gone too, so the type was created empty and never reindexed.
        return resources_alias_existed


def create_latest_indices_and_reindex(props: ManageMappingsProps):
    # Decide once, before this run creates any index.
    resources_alias_existed = resources_alias_exists(props)
    collections_to_reindex = []
    for type_alias, current_index_name in props.type_alias_to_current_index_name.items():
        if not props.opensearch_client.indices.exists(current_index_name):
            # Check before creating new index.
            if should_reindex_collection(props, resources_alias_existed):
                collections_to_reindex.append(type_alias)
            create_index(
                props=props,
                type_alias=type_alias,
                current_index_name=current_index_name,
            )
        else:
            print(f'Index {current_index_name} for {type_alias} already exists')
    reindex_collections(props, collections_to_reindex)


def get_current_index_names(type_alias_to_current_index_name):
    return [
        current_index_name
        for type_alias, current_index_name in type_alias_to_current_index_name.items()
    ]


def get_type_aliases(props: ManageMappingsProps, index_aliases):
    # Every index we create carries its type alias plus the shared resources
    # alias. Strip the shared one to get the type alias(es) of an index.
    return sorted(
        alias
        for alias in index_aliases
        if alias != props.all_resources_alias
    )


def has_only_unregistered_types(props: ManageMappingsProps, type_aliases):
    return all(
        type_alias not in props.type_alias_to_current_index_name
        for type_alias in type_aliases
    )


def delete_index(props: ManageMappingsProps, index):
    print(
        props.opensearch_client.indices.delete(
            index=index,
            ignore=[400, 404],
        )
    )


def delete_old_indices(props: ManageMappingsProps):
    current_index_names = get_current_index_names(
        props.type_alias_to_current_index_name
    )
    # This must stay the index= form (GET /<alias>/_alias), which returns
    # every alias of each index behind the resources alias. The name= form
    # (GET /_alias/<alias>) returns only the resources alias itself and would
    # hide the type aliases the classification below depends on.
    all_existing_resources_indices = props.opensearch_client.indices.get_alias(
        index=props.all_resources_alias,
    )
    for existing_index in sorted(all_existing_resources_indices):
        if existing_index in current_index_names:
            print(f'Not deleting current index {existing_index}')
            continue
        type_aliases = get_type_aliases(
            props,
            all_existing_resources_indices[existing_index].get('aliases', {}),
        )
        if type_aliases and has_only_unregistered_types(props, type_aliases):
            # The type was removed or renamed. No reindex will ever move its
            # documents into a current index, so without this the index would
            # stay in the resources alias forever and keep feeding stale
            # documents to invalidation.
            print(
                f'{existing_index} belongs to unregistered type(s) '
                f'{type_aliases}, deleting'
            )
            delete_index(props, existing_index)
            props.summary['deleted_removed_types'].append(existing_index)
            continue
        # Either an older index of a registered type, or an index with no type
        # alias at all: the legacy create-mapping command only set the
        # resources alias, and someone may have made one by hand. We can't
        # tell what the latter holds, so both keep the old rule: delete once
        # empty, otherwise leave it and report it.
        documents_in_index = props.opensearch_client.count(index=existing_index)['count']
        if documents_in_index != 0:
            print(f'{existing_index} is not latest but still has documents, skipping delete')
            if type_aliases:
                props.summary['skipped_not_empty'].append(existing_index)
            else:
                props.summary['skipped_no_type_alias'].append(existing_index)
            continue
        print(f'Deleting {existing_index}')
        delete_index(props, existing_index)
        props.summary['deleted'].append(existing_index)


def update(props: ManageMappingsProps):
    clean_up_auto_created_indices(props)
    create_latest_indices_and_reindex(props)
    delete_old_indices(props)


def get_type_alias_to_current_index_name(app):
    item_type_to_index_name = app.registry['OPENSEARCH_ITEM_TYPE_TO_INDEX_NAME']
    return item_type_to_index_name


def get_mappings(relative_mapping_directory, type_alias_to_current_index_name):
    current_directory = os.getcwd()
    mapping_directory = Path(current_directory, relative_mapping_directory)
    mappings = {}
    for index_type, index_name in type_alias_to_current_index_name.items():
        print('Reading mapping for', index_type)
        filename = f'{index_type}.json'
        with open(Path(mapping_directory, filename), 'r') as f:
            mappings[index_type] = json.load(f)['mapping']
    return mappings


def make_summary():
    return {
        'created': [],
        'deleted': [],
        'deleted_removed_types': [],
        'reindexed': [],
        'cleaned_up': [],
        'skipped_not_empty': [],
        'skipped_no_type_alias': [],
    }


def manage_mappings(app, relative_mapping_directory, should_reindex='never'):
    type_alias_to_current_index_name = get_type_alias_to_current_index_name(app)
    mappings = get_mappings(
        relative_mapping_directory=relative_mapping_directory,
        type_alias_to_current_index_name=type_alias_to_current_index_name,
    )
    props = ManageMappingsProps(
        app=app,
        opensearch_client=app.registry[ELASTIC_SEARCH],
        type_alias_to_current_index_name=type_alias_to_current_index_name,
        mappings=mappings,
        all_resources_alias=ALL_RESOURCES_ALIAS,
        should_reindex=should_reindex,
        summary=make_summary(),
    )
    update(props)
    return props.summary


def get_args():
    import argparse
    parser = argparse.ArgumentParser(
        description='Manage Opensearch mappings',
    )
    parser.add_argument(
        '--app-name',
        help='Pyramid app name in configfile'
    )
    parser.add_argument(
        'config_uri',
        help='path to configfile'
    )
    parser.add_argument(
        '--should-reindex',
        help='Controls reindexing behavior after creating new index',
        choices=[
            'always',
            'never',
            'after-initial',
        ],
        default='always',
    )
    parser.add_argument(
        '--relative-mapping-directory',
        help='Directory to read mappings'
    )
    return parser.parse_args()


def main():
    args = get_args()
    app = get_app(
        args.config_uri,
        args.app_name
    )
    summary = manage_mappings(
        app=app,
        relative_mapping_directory=args.relative_mapping_directory,
        should_reindex=args.should_reindex
    )
    print(summary)


if __name__ == '__main__':
    main()
