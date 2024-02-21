from graphql import GraphQLField, GraphQLObjectType, GraphQLSchema, GraphQLString


class DisableIntrospectionMiddleware:
    """
    This class hides the introspection.
    """
    def resolve(self, next, root, info, **kwargs):
        if info.field_name.lower() in ['__schema', '_introspection']:
            query = GraphQLObjectType(
                "Query", lambda: {"Hello": GraphQLField(GraphQLString, resolver=lambda *_: "World")}
            )
            info.schema = GraphQLSchema(query=query)
            return next(root, info, **kwargs)
        return next(root, info, **kwargs)