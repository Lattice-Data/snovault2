import pytest

pytestmark = [pytest.mark.indexing]


def test_manage_mappings_deletes_index_of_unregistered_type(app, workbook):
    from snovault.elasticsearch.interfaces import ELASTIC_SEARCH
    from snovault.elasticsearch.interfaces import RESOURCES_INDEX
    from snovault.elasticsearch.manage_mappings import manage_mappings
    os = app.registry[ELASTIC_SEARCH]
    # An index of a type this app does not register, holding a document:
    # the case behind DB2-85.
    os.indices.create(
        index='obsolete_type_0000',
        body={'aliases': {'obsolete_type': {}, RESOURCES_INDEX: {}}},
    )
    os.index(index='obsolete_type_0000', id='1', body={'uuid': '1'}, refresh=True)
    # A non-empty index with only the resources alias, as the legacy
    # create-mapping command made them.
    os.indices.create(
        index='legacy_resources_only',
        body={'aliases': {RESOURCES_INDEX: {}}},
    )
    os.index(index='legacy_resources_only', id='1', body={'uuid': '1'}, refresh=True)
    try:
        summary = manage_mappings(
            app=app,
            relative_mapping_directory='src/snowflakes/mappings',
            should_reindex='never',
        )
        assert summary['deleted_removed_types'] == ['obsolete_type_0000']
        assert summary['skipped_no_type_alias'] == ['legacy_resources_only']
        assert summary['created'] == []
        assert summary['deleted'] == []
        assert not os.indices.exists('obsolete_type_0000')
        assert os.indices.exists('legacy_resources_only')
    finally:
        os.indices.delete(index='legacy_resources_only', ignore=[404])
        os.indices.delete(index='obsolete_type_0000', ignore=[404])
