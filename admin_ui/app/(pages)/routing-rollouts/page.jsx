import RoutingRolloutsConsole from '../../components/RoutingRolloutsConsole';

export default async function RoutingRolloutsPage({ searchParams }) {
  const resolvedSearchParams = await searchParams;
  const collection = typeof resolvedSearchParams?.collection === 'string'
    ? resolvedSearchParams.collection
    : '';
  return <RoutingRolloutsConsole initialCollection={collection} />;
}
