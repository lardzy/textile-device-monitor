import { BrowserRouter as Router, Routes, Route, useLocation, useNavigate } from 'react-router-dom';
import { Layout, Menu } from 'antd';
import { MonitorOutlined, HistoryOutlined, BarChartOutlined, SettingOutlined } from '@ant-design/icons';
import DeviceMonitor from './pages/DeviceMonitor';
import HistoryQuery from './pages/HistoryQuery';
import Statistics from './pages/Statistics';
import DeviceManagement from './pages/DeviceManagement';
import wsClient from './websocket/client';
import { useState, useEffect } from 'react';

const { Header, Content, Sider } = Layout;

const menuItems = [
  { key: 'monitor', icon: <MonitorOutlined />, label: '设备监控', path: '/' },
  { key: 'history', icon: <HistoryOutlined />, label: '历史记录', path: '/history' },
  { key: 'statistics', icon: <BarChartOutlined />, label: '数据统计', path: '/statistics' },
  { key: 'management', icon: <SettingOutlined />, label: '设备管理', path: '/management' },
];

function AppLayout() {
  const [collapsed, setCollapsed] = useState(false);
  const navigate = useNavigate();
  const location = useLocation();

  const selectedKey = menuItems.find(item => item.path === location.pathname)?.key || 'monitor';

  useEffect(() => {
    const baseUrl = import.meta.env.VITE_WS_URL;
    const wsUrl = baseUrl
      || `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws`;
    wsClient.connect(wsUrl);

    return () => {
      wsClient.disconnect();
    };
  }, []);

  const handleMenuClick = ({ key }) => {
    const target = menuItems.find(item => item.key === key);
    if (target) {
      navigate(target.path);
    }
  };

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider collapsible collapsed={collapsed} onCollapse={setCollapsed} theme="dark">
        <div style={{ padding: '16px', color: 'white', textAlign: 'center', fontSize: '18px', fontWeight: 'bold' }}>
          {collapsed ? '检测' : '纺织品检测系统'}
        </div>
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={[selectedKey]}
          items={menuItems}
          onClick={handleMenuClick}
        />
      </Sider>
      <Layout>
        <Header style={{ background: '#fff', display: 'flex', alignItems: 'center', padding: '0 24px', boxShadow: '0 2px 8px rgba(0,0,0,0.1)' }}>
          <h2 style={{ margin: 0 }}>{menuItems.find(item => item.key === selectedKey)?.label}</h2>
        </Header>
        <Content style={{ margin: '24px 16px', padding: 24, background: '#fff', borderRadius: '8px' }}>
          <Routes>
            <Route path="/" element={<DeviceMonitor />} />
            <Route path="/history" element={<HistoryQuery />} />
            <Route path="/statistics" element={<Statistics />} />
            <Route path="/management" element={<DeviceManagement />} />
          </Routes>
        </Content>
      </Layout>
    </Layout>
  );
}

function App() {
  return (
    <Router>
      <AppLayout />
    </Router>
  );
}

export default App;
