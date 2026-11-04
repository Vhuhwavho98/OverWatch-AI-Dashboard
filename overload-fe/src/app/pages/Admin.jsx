import Sidebar from '../components/Sidebar';
import Header from '../components/Header';

export default function Admin() {
  return (
    <div className='layout'>
      <Sidebar />

      <main className='content'>
        <Header />

        <section className='panel'>
          <h2>System Administration</h2>

          <div className='admin-grid'>
            <button>Restart Camera</button>
            <button>Restart Detection Engine</button>
            <button>Refresh Database</button>
            <button>Download Detection Report</button>
          </div>
        </section>
      </main>
    </div>
  );
}