import { describe, expect, it } from 'vitest';
import {
  hasNativeRendererContract,
  resolveTrustedHumanRenderer,
  trustedHumanRendererCapabilities,
} from './executionHumanRenderers';

const trusted = {
  capability: 'human.select',
  version: '1.0.0',
  protocol: 'native.select.v1',
  contract_digest: 'e355eec60016099fda22268ccd7a02b805ab77682cbee8e6b96c9930f9af9a41',
};

describe('trusted Execution v2 Human renderer registry', () => {
  it('resolves only an exact capability, protocol, version and digest', () => {
    expect(resolveTrustedHumanRenderer(trusted)).toMatchObject({
      capability: 'human.select',
      protocol: 'native.select.v1',
    });
    expect(resolveTrustedHumanRenderer({
      ...trusted,
      contract_digest: '0'.repeat(64),
    })).toBeNull();
    expect(resolveTrustedHumanRenderer({
      ...trusted,
      protocol: 'native.select.v2',
    })).toBeNull();
    expect(resolveTrustedHumanRenderer({
      capability: 'unknown.renderer',
      version: '1.0.0',
      protocol: 'unknown.v1',
      contract_digest: '0'.repeat(64),
    })).toBeNull();
  });

  it('fails closed for incomplete native contracts and exposes all P2 renderers', () => {
    expect(hasNativeRendererContract({ capability: 'human.form' })).toBe(true);
    expect(resolveTrustedHumanRenderer({ capability: 'human.form' })).toBeNull();
    expect(hasNativeRendererContract({})).toBe(false);
    expect(trustedHumanRendererCapabilities().map(item => item.capability)).toEqual([
      'human.form',
      'human.select',
      'human.approval',
      'human.decision',
      'file.batch_place.conflict',
    ]);
  });
});
