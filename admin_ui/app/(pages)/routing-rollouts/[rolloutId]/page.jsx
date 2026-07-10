import RoutingRolloutsConsole from '../../../components/RoutingRolloutsConsole';

export default async function RoutingRolloutDetailPage({ params }) {
  const resolvedParams = await params;
  const rolloutId = typeof resolvedParams?.rolloutId === 'string'
    ? resolvedParams.rolloutId
    : '';
  return <RoutingRolloutsConsole initialRolloutId={rolloutId} />;
}
