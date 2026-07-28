import { describe, expect, it } from 'vitest';
import { getDefaultRenameName } from './folderRename';

describe('getDefaultRenameName', () => {
  it('保留第一个下划线前的内容', () => {
    expect(getDefaultRenameName('260144785_辜_面积法')).toBe('260144785');
  });

  it('没有下划线时保留完整目录名', () => {
    expect(getDefaultRenameName('260144785')).toBe('260144785');
  });

  it('与设备监控原有规则一致地处理空白和首字符下划线', () => {
    expect(getDefaultRenameName('  260162847_根数法  ')).toBe('260162847');
    expect(getDefaultRenameName('_archive')).toBe('_archive');
    expect(getDefaultRenameName(null)).toBe('');
  });
});
