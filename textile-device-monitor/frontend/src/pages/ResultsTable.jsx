import { useEffect, useRef, useState } from 'react';
import { Button, Card, Select, Spin, message } from 'antd';
import Spreadsheet from 'x-data-spreadsheet';
import 'x-data-spreadsheet/dist/xspreadsheet.css';
import * as XLSX from 'xlsx';

function ResultsTable() {
  const [loading, setLoading] = useState(true);
  const spreadsheetRef = useRef(null);
  const spreadsheetInstance = useRef(null);
  const [sheetData, setSheetData] = useState(null);
  const [sheets, setSheets] = useState([]);
  const [selectedSheet, setSelectedSheet] = useState(null);
  const [hasData, setHasData] = useState(true);

  const params = new URLSearchParams(window.location.search);
  const deviceId = params.get('device_id');

  useEffect(() => {
    const fetchTable = async () => {
      if (!deviceId) {
        message.error('缺少设备参数');
        setLoading(false);
        return;
      }

      try {
        const response = await fetch(`/api/results/table?device_id=${deviceId}`);
        if (!response.ok) {
          throw new Error('表格获取失败');
        }
        const arrayBuffer = await response.arrayBuffer();
        const workbook = XLSX.read(arrayBuffer, { type: 'array' });
        workbookRef.current = workbook;
        const sheetNames = workbook.SheetNames || [];

        if (sheetNames.length === 0) {
          setHasData(false);
          setSheetData(null);
          setSheets([]);
          setSelectedSheet(null);
        } else {
          setSheets(sheetNames);
          setHasData(true);
          const initialSheet = sheetNames[0];
          setSelectedSheet(initialSheet);
          setSheetData(buildSheetData(workbook, initialSheet));
        }
      } catch (error) {
        message.error('表格加载失败');
      } finally {
        setLoading(false);
      }
    };

    fetchTable();
  }, [deviceId]);

  const buildSheetData = (workbook, name) => {
    const sheet = workbook.Sheets[name];
    const jsonData = XLSX.utils.sheet_to_json(sheet, { header: 1 });
    const rows = {};
    jsonData.forEach((row, rowIndex) => {
      const cells = {};
      row.forEach((cell, cellIndex) => {
        if (cell == null) return;
        cells[cellIndex] = { text: String(cell) };
      });
      rows[rowIndex] = { cells };
    });
    return { name, rows };
  };

  const workbookRef = useRef(null);

  useEffect(() => {
    if (!spreadsheetRef.current || !sheetData || loading) {
      return;
    }

    if (!spreadsheetInstance.current) {
      spreadsheetRef.current.innerHTML = '';
      spreadsheetInstance.current = new Spreadsheet(spreadsheetRef.current, {
        showToolbar: true,
        showGrid: true,
        showContextmenu: false,
        showBottomBar: false,
        view: {
          height: () => 600,
          width: () => spreadsheetRef.current?.clientWidth || 800,
        },
      });
      spreadsheetInstance.current.on('sheet-change', (name) => {
        if (name && name !== selectedSheet) {
          setSelectedSheet(name);
        }
      });
    }

    if (typeof spreadsheetInstance.current.loadData === 'function') {
      spreadsheetInstance.current.loadData([sheetData]);
    }
  }, [sheetData, loading, selectedSheet]);

  useEffect(() => {
    return () => {
      if (spreadsheetInstance.current) {
        if (typeof spreadsheetInstance.current.destroy === 'function') {
          spreadsheetInstance.current.destroy();
        }
        spreadsheetInstance.current = null;
      }
    };
  }, []);

  const handleDownload = () => {
    if (!deviceId) return;
    window.open(`/api/results/table?device_id=${deviceId}`, '_blank');
  };

  const handleSheetChange = (value) => {
    if (!workbookRef.current) return;
    setSelectedSheet(value);
    setSheetData(buildSheetData(workbookRef.current, value));
  };

  const sheetSelect = (
    <Select
      value={selectedSheet || undefined}
      placeholder="选择工作表"
      onChange={handleSheetChange}
      style={{ width: 180 }}
      options={sheets.map(name => ({ label: name, value: name }))}
    />
  );

  return (
    <div style={{ padding: 24 }}>
      <Card
        title="结果表格"
        extra={(
          <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            {sheetSelect}
            <Button onClick={handleDownload}>下载表格</Button>
          </div>
        )}
      >
        {loading ? (
          <div style={{ textAlign: 'center', padding: 40 }}>
            <Spin />
          </div>
        ) : hasData ? (
          <div
            ref={spreadsheetRef}
            style={{ height: 600, width: '100%', border: '1px solid #f0f0f0' }}
          />
        ) : (
          <div style={{ textAlign: 'center', padding: 40, color: '#999' }}>
            暂无表格数据
          </div>
        )}
      </Card>
    </div>
  );
}

export default ResultsTable;
