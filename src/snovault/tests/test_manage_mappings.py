from unittest.mock import MagicMock

import snovault.elasticsearch.manage_mappings as manage_mappings_module

from snovault.elasticsearch.manage_mappings import ManageMappingsProps
from snovault.elasticsearch.manage_mappings import create_latest_indices_and_reindex
from snovault.elasticsearch.manage_mappings import delete_old_indices
from snovault.elasticsearch.manage_mappings import make_summary


RESOURCES = 'resources'

CURRENT = {
    'snowflake': 'snowflake_1111',
    'lab': 'lab_2222',
}


def make_opensearch_client(aliased_indices, document_counts):
    # aliased_indices mirrors indices.get_alias(index='resources'):
    # {index_name: {'aliases': {alias: {}, ...}}}
    client = MagicMock()
    client.indices.get_alias.return_value = {
        index: {'aliases': {alias: {} for alias in aliases}}
        for index, aliases in aliased_indices.items()
    }
    client.count.side_effect = lambda index: {'count': document_counts[index]}
    return client


def make_props(opensearch_client, type_alias_to_current_index_name=CURRENT, should_reindex='never'):
    return ManageMappingsProps(
        app=None,
        opensearch_client=opensearch_client,
        type_alias_to_current_index_name=type_alias_to_current_index_name,
        mappings={},
        all_resources_alias=RESOURCES,
        should_reindex=should_reindex,
        summary=make_summary(),
    )


def deleted_indices(client):
    return [
        call.kwargs['index']
        for call in client.indices.delete.call_args_list
    ]


def test_manage_mappings_make_summary_keys():
    assert sorted(make_summary()) == [
        'cleaned_up',
        'created',
        'deleted',
        'deleted_removed_types',
        'reindexed',
        'skipped_no_type_alias',
        'skipped_not_empty',
    ]


def test_manage_mappings_delete_old_indices_reads_aliases_with_index_form():
    # GET /<alias>/_alias returns every alias per index; GET /_alias/<alias>
    # would return only the resources alias and hide the type aliases.
    client = make_opensearch_client(
        {'snowflake_1111': ['snowflake', RESOURCES]},
        {'snowflake_1111': 10},
    )
    delete_old_indices(make_props(client))
    client.indices.get_alias.assert_called_once_with(index=RESOURCES)


def test_manage_mappings_delete_old_indices_keeps_current_index():
    client = make_opensearch_client(
        {'snowflake_1111': ['snowflake', RESOURCES]},
        {'snowflake_1111': 10},
    )
    props = make_props(client)
    delete_old_indices(props)
    assert deleted_indices(client) == []
    assert props.summary == make_summary()


def test_manage_mappings_delete_old_indices_skips_old_index_of_registered_type_with_documents():
    client = make_opensearch_client(
        {
            'snowflake_1111': ['snowflake', RESOURCES],
            'snowflake_0000': ['snowflake', RESOURCES],
        },
        {'snowflake_1111': 10, 'snowflake_0000': 3},
    )
    props = make_props(client)
    delete_old_indices(props)
    assert deleted_indices(client) == []
    assert props.summary['skipped_not_empty'] == ['snowflake_0000']
    assert props.summary['deleted'] == []
    assert props.summary['deleted_removed_types'] == []


def test_manage_mappings_delete_old_indices_deletes_empty_old_index_of_registered_type():
    client = make_opensearch_client(
        {
            'snowflake_1111': ['snowflake', RESOURCES],
            'snowflake_0000': ['snowflake', RESOURCES],
        },
        {'snowflake_1111': 10, 'snowflake_0000': 0},
    )
    props = make_props(client)
    delete_old_indices(props)
    assert deleted_indices(client) == ['snowflake_0000']
    assert props.summary['deleted'] == ['snowflake_0000']
    assert props.summary['skipped_not_empty'] == []
    assert props.summary['deleted_removed_types'] == []


def test_manage_mappings_delete_old_indices_deletes_index_of_unregistered_type_with_documents():
    # A type removed from the app: no reindex will ever drain its index.
    client = make_opensearch_client(
        {
            'snowflake_1111': ['snowflake', RESOURCES],
            'treatment_8358168e': ['treatment', RESOURCES],
        },
        {'snowflake_1111': 10, 'treatment_8358168e': 4},
    )
    props = make_props(client)
    delete_old_indices(props)
    assert deleted_indices(client) == ['treatment_8358168e']
    assert props.summary['deleted_removed_types'] == ['treatment_8358168e']
    assert props.summary['deleted'] == []
    assert props.summary['skipped_not_empty'] == []
    # The document count does not matter for a removed type.
    client.count.assert_not_called()


def test_manage_mappings_delete_old_indices_keeps_non_empty_index_with_only_resources_alias():
    # Legacy create-mapping indices carry only the resources alias.
    client = make_opensearch_client(
        {
            'snowflake_1111': ['snowflake', RESOURCES],
            'legacy_index': [RESOURCES],
        },
        {'snowflake_1111': 10, 'legacy_index': 7},
    )
    props = make_props(client)
    delete_old_indices(props)
    assert deleted_indices(client) == []
    assert props.summary['skipped_no_type_alias'] == ['legacy_index']
    assert props.summary['skipped_not_empty'] == []
    assert props.summary['deleted_removed_types'] == []


def test_manage_mappings_delete_old_indices_deletes_empty_index_with_only_resources_alias():
    client = make_opensearch_client(
        {
            'snowflake_1111': ['snowflake', RESOURCES],
            'legacy_index': [RESOURCES],
        },
        {'snowflake_1111': 10, 'legacy_index': 0},
    )
    props = make_props(client)
    delete_old_indices(props)
    assert deleted_indices(client) == ['legacy_index']
    assert props.summary['deleted'] == ['legacy_index']
    assert props.summary['skipped_no_type_alias'] == []


def test_manage_mappings_delete_old_indices_treats_mixed_aliases_as_registered_type():
    client = make_opensearch_client(
        {
            'snowflake_1111': ['snowflake', RESOURCES],
            'odd_0000': ['snowflake', 'obsolete', RESOURCES],
        },
        {'snowflake_1111': 10, 'odd_0000': 2},
    )
    props = make_props(client)
    delete_old_indices(props)
    assert deleted_indices(client) == []
    assert props.summary['skipped_not_empty'] == ['odd_0000']
    assert props.summary['deleted_removed_types'] == []


def run_create_latest_indices_and_reindex(monkeypatch, should_reindex, resources_alias_exists, existing_indices=()):
    client = MagicMock()
    client.indices.exists.side_effect = lambda index: index in existing_indices
    client.indices.exists_alias.return_value = resources_alias_exists
    props = make_props(client, should_reindex=should_reindex)
    created = []
    reindexed = []
    monkeypatch.setattr(
        manage_mappings_module,
        'create_index',
        lambda props, type_alias, current_index_name: created.append(current_index_name),
    )
    monkeypatch.setattr(
        manage_mappings_module,
        'reindex_collections',
        lambda props, collections: reindexed.extend(collections),
    )
    create_latest_indices_and_reindex(props)
    client.indices.exists_alias.assert_called_once_with(name=RESOURCES)
    return created, reindexed


def test_manage_mappings_create_latest_indices_after_initial_skips_reindex_on_first_run(monkeypatch):
    created, reindexed = run_create_latest_indices_and_reindex(
        monkeypatch,
        should_reindex='after-initial',
        resources_alias_exists=False,
    )
    assert created == ['snowflake_1111', 'lab_2222']
    assert reindexed == []


def test_manage_mappings_create_latest_indices_after_initial_reindexes_new_index_in_existing_cluster(monkeypatch):
    # Covers a type that comes back after delete_old_indices removed its
    # index: its own alias is gone, but the cluster already has indices.
    created, reindexed = run_create_latest_indices_and_reindex(
        monkeypatch,
        should_reindex='after-initial',
        resources_alias_exists=True,
        existing_indices=['snowflake_1111'],
    )
    assert created == ['lab_2222']
    assert reindexed == ['lab']


def test_manage_mappings_create_latest_indices_always_reindexes_created_indices(monkeypatch):
    created, reindexed = run_create_latest_indices_and_reindex(
        monkeypatch,
        should_reindex='always',
        resources_alias_exists=False,
        existing_indices=['snowflake_1111'],
    )
    assert created == ['lab_2222']
    assert reindexed == ['lab']


def test_manage_mappings_create_latest_indices_never_reindexes(monkeypatch):
    created, reindexed = run_create_latest_indices_and_reindex(
        monkeypatch,
        should_reindex='never',
        resources_alias_exists=True,
    )
    assert created == ['snowflake_1111', 'lab_2222']
    assert reindexed == []
