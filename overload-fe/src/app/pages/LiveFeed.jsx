import Sidebar from '../components/Sidebar';
import Header from '../components/Header';

export default function LiveFeed() {
  return (
    <div className='layout'>
      <Sidebar />

      <main className='content'>
        <Header />

        <section className='panel'>
          <h2>Live Camera Feed</h2>

          <img
            src='http://127.0.0.1:8000/video_feed'
            alt='Live Camera Feed'
            className='camera-feed'
          />
        </section>
      </main>
    </div>
  );
}