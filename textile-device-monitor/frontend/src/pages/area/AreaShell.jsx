import {
  FolderOpenOutlined,
  PlayCircleOutlined,
  SettingOutlined,
  UnorderedListOutlined,
} from '@ant-design/icons';
import { Button, Result, Segmented, Space, Spin } from 'antd';
import { useEffect, useState } from 'react';
import { Outlet, useLocation, useNavigate } from 'react-router-dom';
import { areaApi } from '../../api/area';
import './area.css';

const NAV_ITEMS = [
  { value: '/tools/area', label: '开始识别', icon: <PlayCircleOutlined /> },
  { value: '/tools/area/tasks', label: '任务记录', icon: <UnorderedListOutlined /> },
  { value: '/tools/area/folders', label: '数据目录', icon: <FolderOpenOutlined /> },
];

function AreaShell() {
  const location = useLocation();
  const navigate = useNavigate();
  // checking → enabled / disabled；禁用时整个模块只显示说明页，
  // 避免各子页面分别报出 area_disabled 原始错误码
  const [moduleState, setModuleState] = useState('checking');
  const isWorkspace = location.pathname.startsWith('/tools/area/jobs/');
  const selected = location.pathname.startsWith('/tools/area/settings')
    ? null
    : (
      location.pathname.startsWith('/tools/area/tasks')
        ? '/tools/area/tasks'
        : (location.pathname.startsWith('/tools/area/folders') ? '/tools/area/folders' : '/tools/area')
    );

  useEffect(() => {
    let active = true;
    areaApi.getStatus()
      .then(() => {
        if (active) setModuleState('enabled');
      })
      .catch((error) => {
        if (!active) return;
        setModuleState(error?.message === 'area_disabled' ? 'disabled' : 'enabled');
      });
    return () => {
      active = false;
    };
  }, []);

  if (moduleState === 'disabled') {
    return (
      <div className="area-shell">
        <Result
          status="info"
          title="面积识别未启用"
          subTitle="本次部署未开放面积识别模块；如需使用，请联系管理员开启。"
        />
      </div>
    );
  }

  return (
    <div className={isWorkspace ? 'area-shell area-shell--workspace' : 'area-shell'}>
      {!isWorkspace ? (
        <div className="area-section-nav">
          <Segmented
            value={selected}
            options={NAV_ITEMS}
            onChange={(value) => navigate(value)}
          />
          <Space>
            <Button
              icon={<SettingOutlined />}
              onClick={() => navigate('/tools/area/settings')}
            >
              全局设置
            </Button>
          </Space>
        </div>
      ) : null}
      {moduleState === 'checking' ? (
        <div className="area-shell-loading"><Spin size="large" /></div>
      ) : (
        <Outlet />
      )}
    </div>
  );
}

export default AreaShell;
