export function LoadingState() {
  return (
    <main className="grid h-full min-h-0 grid-cols-1 bg-pir-base text-pir-text-primary lg:grid-cols-[380px_minmax(0,1fr)]">
      <section className="border-r border-pir bg-pir-surface-0">
        <div className="border-b border-pir px-4 py-3">
          <div className="h-5 w-32 animate-pulse rounded-sm bg-pir-surface-2" />
          <div className="mt-2 h-3 w-56 animate-pulse rounded-sm bg-pir-surface-2" />
        </div>
        <div className="divide-y divide-pir">
          {Array.from({ length: 8 }).map((_, index) => (
            <div key={index} className="flex gap-3 px-4 py-3">
              <div className="h-10 w-8 animate-pulse rounded-sm bg-pir-surface-2" />
              <div className="flex-1 space-y-2">
                <div className="h-3 w-5/6 animate-pulse rounded-sm bg-pir-surface-2" />
                <div className="h-3 w-1/2 animate-pulse rounded-sm bg-pir-surface-2" />
                <div className="h-2 w-2/3 animate-pulse rounded-sm bg-pir-surface-2" />
              </div>
            </div>
          ))}
        </div>
      </section>
      <section className="flex min-h-0 flex-col bg-pir-base">
        <div className="border-b border-pir bg-pir-surface-0 px-5 py-4">
          <div className="h-6 w-1/2 animate-pulse rounded-sm bg-pir-surface-2" />
          <div className="mt-2 h-3 w-2/3 animate-pulse rounded-sm bg-pir-surface-2" />
        </div>
        <div className="grid min-h-0 flex-1 grid-cols-1 xl:grid-cols-[minmax(0,1fr)_320px]">
          <div className="p-5">
            <div className="h-full min-h-[420px] animate-pulse rounded-sm border border-pir bg-pir-surface-0" />
          </div>
          <aside className="border-t border-pir bg-pir-surface-0 p-4 xl:border-l xl:border-t-0">
            {Array.from({ length: 6 }).map((_, index) => (
              <div key={index} className="mb-4">
                <div className="h-3 w-20 animate-pulse rounded-sm bg-pir-surface-2" />
                <div className="mt-2 h-4 w-full animate-pulse rounded-sm bg-pir-surface-2" />
              </div>
            ))}
          </aside>
        </div>
      </section>
    </main>
  );
}
