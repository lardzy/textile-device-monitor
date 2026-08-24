const TRUSTED_RENDERERS = new Map([
  ['human.form@1.0.0', {
    capability: 'human.form',
    version: '1.0.0',
    protocol: 'native.form.v1',
    contractDigest: 'a9562673b95a340869b1a8244bcf97b30675f25225591f716ba72e74615db880',
  }],
  ['human.select@1.0.0', {
    capability: 'human.select',
    version: '1.0.0',
    protocol: 'native.select.v1',
    contractDigest: 'e355eec60016099fda22268ccd7a02b805ab77682cbee8e6b96c9930f9af9a41',
  }],
  ['human.approval@1.0.0', {
    capability: 'human.approval',
    version: '1.0.0',
    protocol: 'native.approval.v1',
    contractDigest: 'c65127408d47900c4b04fb4e7da58b629d6507e6e48d2a86f4949e6fc342564b',
  }],
  ['human.decision@1.0.0', {
    capability: 'human.decision',
    version: '1.0.0',
    protocol: 'native.decision.v1',
    contractDigest: '1d2a4c76c849ff69a44d604e96ac6ca4c17c14a2125f511d734022defb6d393d',
  }],
  ['file.batch_place.conflict@1.0.0', {
    capability: 'file.batch_place.conflict',
    version: '1.0.0',
    protocol: 'retry_with_decision.v1',
    contractDigest: '946e87763a41d59eb350d69cd2591c21e9207c1edd0d2c79a8c6975a8f1778c6',
  }],
]);

export const hasNativeRendererContract = contract => Boolean(
  contract?.capability || contract?.version || contract?.contract_digest,
);

export const resolveTrustedHumanRenderer = (contract) => {
  if (!hasNativeRendererContract(contract)) {
    return null;
  }
  const renderer = TRUSTED_RENDERERS.get(
    `${contract.capability}@${contract.version}`,
  );
  if (
    !renderer
    || renderer.protocol !== contract.protocol
    || renderer.contractDigest !== contract.contract_digest
  ) {
    return null;
  }
  return renderer;
};

export const trustedHumanRendererCapabilities = () => (
  Array.from(TRUSTED_RENDERERS.values())
);
