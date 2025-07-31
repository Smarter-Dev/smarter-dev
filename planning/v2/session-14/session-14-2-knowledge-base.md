# Session 14.2: Knowledge Base System - Organized Learning Resources

## Overview
Implement a comprehensive knowledge base system with FAQ management, content categorization, advanced search functionality, and community-contributed content support.

## Key Components
- FAQ system and documentation organization
- Content categorization and tagging
- Advanced search functionality with filters
- Content versioning and history tracking
- Community-contributed content workflows

## Implementation Details

### Knowledge Base Service
Core service for knowledge base operations:

```python
from typing import List, Dict, Any, Optional
from sqlalchemy import select, and_, or_
from web.models.knowledge import FAQ, Category, Tag, ContentRevision
from web.crud.knowledge import knowledge_crud

class KnowledgeBaseService:
    """Service for knowledge base management."""
    
    def __init__(self, db_session, search_engine):
        self.db = db_session
        self.search = search_engine
    
    async def create_faq(
        self,
        question: str,
        answer: str,
        category_id: str,
        author_id: str,
        tags: List[str] = None
    ) -> Dict[str, Any]:
        """Create a new FAQ entry."""
        try:
            faq = await knowledge_crud.create_faq(
                db=self.db,
                question=question,
                answer=answer,
                category_id=category_id,
                author_id=author_id,
                tags=tags or []
            )
            
            # Index for search
            await self.search.index_document(
                doc_id=str(faq.id),
                doc_type="faq",
                content={
                    "question": question,
                    "answer": answer,
                    "tags": tags or [],
                    "category_id": category_id
                }
            )
            
            return {
                "id": str(faq.id),
                "question": faq.question,
                "answer": faq.answer,
                "category_id": faq.category_id,
                "created_at": faq.created_at.isoformat()
            }
            
        except Exception as e:
            logger.error("Failed to create FAQ", error=str(e))
            raise KnowledgeBaseError(f"Failed to create FAQ: {str(e)}")
    
    async def search_knowledge_base(
        self,
        query: str,
        category_id: Optional[str] = None,
        tags: Optional[List[str]] = None,
        content_types: Optional[List[str]] = None,
        limit: int = 20,
        offset: int = 0
    ) -> Dict[str, Any]:
        """Search across all knowledge base content."""
        try:
            # Build search filters
            filters = {}
            if category_id:
                filters["category_id"] = category_id
            if tags:
                filters["tags"] = {"any": tags}
            if content_types:
                filters["doc_type"] = {"any": content_types}
            
            # Execute search
            search_results = await self.search.search(
                query=query,
                filters=filters,
                limit=limit,
                offset=offset,
                highlight=True
            )
            
            # Enrich results with metadata
            enriched_results = []
            for result in search_results.results:
                enriched_result = await self._enrich_search_result(result)
                enriched_results.append(enriched_result)
            
            return {
                "results": enriched_results,
                "total": search_results.total,
                "query": query,
                "filters": filters,
                "facets": search_results.facets
            }
            
        except Exception as e:
            logger.error("Knowledge base search failed", query=query, error=str(e))
            raise KnowledgeBaseError(f"Search failed: {str(e)}")
    
    async def get_category_hierarchy(self) -> List[Dict[str, Any]]:
        """Get hierarchical category structure."""
        categories = await knowledge_crud.get_all_categories(self.db)
        
        # Build hierarchy
        category_tree = self._build_category_tree(categories)
        
        return category_tree
    
    async def suggest_content(
        self,
        user_id: str,
        based_on: str = "reading_history",
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Suggest relevant content based on user behavior."""
        try:
            if based_on == "reading_history":
                suggestions = await self._suggest_from_reading_history(user_id, limit)
            elif based_on == "popular":
                suggestions = await self._suggest_popular_content(limit)
            elif based_on == "recent":
                suggestions = await self._suggest_recent_content(limit)
            else:
                suggestions = await self._suggest_mixed_content(user_id, limit)
            
            return suggestions
            
        except Exception as e:
            logger.error("Content suggestion failed", user_id=user_id, error=str(e))
            return []
```

### Advanced Search Implementation
Sophisticated search with facets and filters:

```python
from elasticsearch import AsyncElasticsearch
from typing import Dict, List, Any

class AdvancedSearchEngine:
    """Advanced search engine for knowledge base."""
    
    def __init__(self, elasticsearch_url: str):
        self.es = AsyncElasticsearch([elasticsearch_url])
        self.index_name = "knowledge_base"
    
    async def setup_index(self):
        """Setup Elasticsearch index with proper mapping."""
        mapping = {
            "mappings": {
                "properties": {
                    "title": {"type": "text", "analyzer": "standard"},
                    "content": {"type": "text", "analyzer": "standard"},
                    "question": {"type": "text", "analyzer": "standard"},
                    "answer": {"type": "text", "analyzer": "standard"},
                    "tags": {"type": "keyword"},
                    "category_id": {"type": "keyword"},
                    "doc_type": {"type": "keyword"},
                    "difficulty": {"type": "keyword"},
                    "created_at": {"type": "date"},
                    "updated_at": {"type": "date"},
                    "view_count": {"type": "integer"},
                    "rating": {"type": "float"}
                }
            }
        }
        
        await self.es.indices.create(
            index=self.index_name,
            body=mapping,
            ignore=400  # Ignore if index already exists
        )
    
    async def search(
        self,
        query: str,
        filters: Dict[str, Any] = None,
        limit: int = 20,
        offset: int = 0,
        highlight: bool = True
    ) -> SearchResults:
        """Execute advanced search with filters and facets."""
        
        # Build query
        search_query = {
            "query": {
                "bool": {
                    "must": [
                        {
                            "multi_match": {
                                "query": query,
                                "fields": ["title^3", "content^2", "question^3", "answer^2", "tags^1.5"],
                                "type": "best_fields",
                                "fuzziness": "AUTO"
                            }
                        }
                    ],
                    "filter": []
                }
            },
            "size": limit,
            "from": offset,
            "sort": [
                {"_score": {"order": "desc"}},
                {"created_at": {"order": "desc"}}
            ]
        }
        
        # Add filters
        if filters:
            for field, value in filters.items():
                if isinstance(value, dict) and "any" in value:
                    search_query["query"]["bool"]["filter"].append({
                        "terms": {field: value["any"]}
                    })
                else:
                    search_query["query"]["bool"]["filter"].append({
                        "term": {field: value}
                    })
        
        # Add highlighting
        if highlight:
            search_query["highlight"] = {
                "fields": {
                    "title": {"fragment_size": 150, "number_of_fragments": 1},
                    "content": {"fragment_size": 150, "number_of_fragments": 3},
                    "question": {"fragment_size": 150, "number_of_fragments": 1},
                    "answer": {"fragment_size": 150, "number_of_fragments": 3}
                }
            }
        
        # Add aggregations for facets
        search_query["aggs"] = {
            "categories": {
                "terms": {"field": "category_id", "size": 20}
            },
            "tags": {
                "terms": {"field": "tags", "size": 50}
            },
            "doc_types": {
                "terms": {"field": "doc_type", "size": 10}
            },
            "difficulty": {
                "terms": {"field": "difficulty", "size": 5}
            }
        }
        
        # Execute search
        response = await self.es.search(
            index=self.index_name,
            body=search_query
        )
        
        return self._parse_search_response(response)
    
    async def index_document(
        self,
        doc_id: str,
        doc_type: str,
        content: Dict[str, Any]
    ):
        """Index a document for search."""
        document = {
            **content,
            "doc_type": doc_type,
            "indexed_at": datetime.utcnow().isoformat()
        }
        
        await self.es.index(
            index=self.index_name,
            id=doc_id,
            body=document
        )
    
    def _parse_search_response(self, response: Dict) -> SearchResults:
        """Parse Elasticsearch response into SearchResults."""
        results = []
        
        for hit in response["hits"]["hits"]:
            result = SearchResult(
                id=hit["_id"],
                score=hit["_score"],
                source=hit["_source"],
                highlight=hit.get("highlight", {})
            )
            results.append(result)
        
        facets = {}
        if "aggregations" in response:
            for facet_name, facet_data in response["aggregations"].items():
                facets[facet_name] = [
                    {"value": bucket["key"], "count": bucket["doc_count"]}
                    for bucket in facet_data["buckets"]
                ]
        
        return SearchResults(
            results=results,
            total=response["hits"]["total"]["value"],
            facets=facets
        )
```

### Content Categorization System
Hierarchical content organization:

```python
class CategoryService:
    """Service for content categorization."""
    
    def __init__(self, db_session):
        self.db = db_session
    
    async def create_category(
        self,
        name: str,
        description: str,
        parent_id: Optional[str] = None,
        icon: Optional[str] = None
    ) -> Dict[str, Any]:
        """Create a new category."""
        category = await knowledge_crud.create_category(
            db=self.db,
            name=name,
            description=description,
            parent_id=parent_id,
            icon=icon
        )
        
        return {
            "id": str(category.id),
            "name": category.name,
            "description": category.description,
            "parent_id": category.parent_id,
            "icon": category.icon,
            "path": await self._get_category_path(category.id)
        }
    
    async def get_category_tree(self) -> List[Dict[str, Any]]:
        """Get complete category hierarchy."""
        categories = await knowledge_crud.get_all_categories(self.db)
        
        # Build tree structure
        category_map = {str(cat.id): cat for cat in categories}
        tree = []
        
        for category in categories:
            if not category.parent_id:  # Root categories
                tree_node = await self._build_category_node(category, category_map)
                tree.append(tree_node)
        
        return tree
    
    async def _build_category_node(
        self,
        category,
        category_map: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Build a single node in the category tree."""
        children = [
            await self._build_category_node(child, category_map)
            for child in category_map.values()
            if child.parent_id == str(category.id)
        ]
        
        return {
            "id": str(category.id),
            "name": category.name,
            "description": category.description,
            "icon": category.icon,
            "children": children,
            "content_count": await self._get_category_content_count(category.id)
        }
    
    async def _get_category_content_count(self, category_id: str) -> int:
        """Get count of content items in category."""
        # Count across all content types
        total_count = 0
        
        # Count FAQs
        faq_count = await knowledge_crud.count_faqs_in_category(self.db, category_id)
        total_count += faq_count
        
        # Count tutorials
        tutorial_count = await knowledge_crud.count_tutorials_in_category(self.db, category_id)
        total_count += tutorial_count
        
        # Count articles
        article_count = await knowledge_crud.count_articles_in_category(self.db, category_id)
        total_count += article_count
        
        return total_count
```

### Community Contribution System
Enable community-driven content creation:

```python
class CommunityContributionService:
    """Service for community-contributed content."""
    
    def __init__(self, db_session, notification_service):
        self.db = db_session
        self.notifications = notification_service
    
    async def submit_contribution(
        self,
        author_id: str,
        content_type: str,
        content_data: Dict[str, Any],
        proposed_category: str
    ) -> Dict[str, Any]:
        """Submit community contribution for review."""
        contribution = await knowledge_crud.create_contribution(
            db=self.db,
            author_id=author_id,
            content_type=content_type,
            content_data=content_data,
            proposed_category=proposed_category,
            status="pending_review"
        )
        
        # Notify moderators
        await self.notifications.notify_moderators(
            "new_contribution",
            {
                "contribution_id": str(contribution.id),
                "author_id": author_id,
                "content_type": content_type,
                "title": content_data.get("title", "Untitled")
            }
        )
        
        return {
            "id": str(contribution.id),
            "status": "pending_review",
            "submitted_at": contribution.created_at.isoformat()
        }
    
    async def review_contribution(
        self,
        contribution_id: str,
        reviewer_id: str,
        decision: str,
        feedback: str = None
    ) -> Dict[str, Any]:
        """Review and approve/reject community contribution."""
        if decision not in ["approved", "rejected", "needs_changes"]:
            raise ValueError("Invalid review decision")
        
        contribution = await knowledge_crud.get_contribution(self.db, contribution_id)
        if not contribution:
            raise ValueError("Contribution not found")
        
        # Update contribution status
        await knowledge_crud.update_contribution(
            db=self.db,
            contribution_id=contribution_id,
            updates={
                "status": decision,
                "reviewer_id": reviewer_id,
                "review_feedback": feedback,
                "reviewed_at": datetime.utcnow()
            }
        )
        
        # If approved, create the actual content
        if decision == "approved":
            content_result = await self._create_approved_content(contribution)
            
            # Award bytes to contributor
            await self._award_contribution_bytes(contribution.author_id, contribution.content_type)
        
        # Notify author
        await self.notifications.notify_user(
            contribution.author_id,
            "contribution_reviewed",
            {
                "contribution_id": contribution_id,
                "decision": decision,
                "feedback": feedback
            }
        )
        
        return {
            "contribution_id": contribution_id,
            "decision": decision,
            "reviewed_by": reviewer_id,
            "reviewed_at": datetime.utcnow().isoformat()
        }
```

## Related Files
- `web/services/knowledge_service.py` - Knowledge base service
- `web/services/search_service.py` - Advanced search implementation
- `web/models/knowledge.py` - Knowledge base models
- `web/api/routes/knowledge.py` - Knowledge base API endpoints
- `web/services/category_service.py` - Category management

## Goals Achieved
- **Organized Knowledge**: Hierarchical categorization system
- **Advanced Search**: Sophisticated search with facets and filters
- **Community Driven**: Enable community contributions with moderation
- **Content Discovery**: Smart content suggestions and recommendations
- **Version Control**: Track content changes and revisions

## Dependencies
- Elasticsearch for advanced search capabilities
- Database models for knowledge organization
- Notification service for community workflow
- Bytes economy integration for contributor rewards
- Moderation tools for content quality

## Testing Strategy
```python
@pytest.mark.asyncio
async def test_knowledge_search():
    """Test knowledge base search functionality."""
    search_engine = AdvancedSearchEngine("http://localhost:9200")
    
    # Index test documents
    await search_engine.index_document(
        doc_id="faq1",
        doc_type="faq",
        content={
            "question": "How to install Python?",
            "answer": "Visit python.org and download the installer",
            "tags": ["python", "installation"],
            "category_id": "python-basics"
        }
    )
    
    # Test search
    results = await search_engine.search(
        query="python install",
        limit=10
    )
    
    assert len(results.results) > 0
    assert "python" in results.results[0].source["tags"]
```

This knowledge base system provides a comprehensive platform for organizing, searching, and discovering educational content with strong community participation features.