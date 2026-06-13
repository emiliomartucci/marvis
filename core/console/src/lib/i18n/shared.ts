export type Locale = "it" | "en";

export interface TranslationDictionary {
  appShell: {
    logoAlt: string;
    navLabel: string;
    nav: {
      diario: string;
      todos: string;
      task: string;
      projects: string;
      universe: string;
    };
    todosBadgeLabel: string;
    quickCaptureLabel: string;
    quickCapturePlaceholder: string;
    quickCaptureToast: string;
    helpLabel: string;
    helpWizard: string;
    helpTour: string;
    localeLabel: string;
    projectsSlotLabel: string;
    projectsSlotEmpty: string;
    localStatus: string;
    hostedSoon: string;
    versionUnavailable: string;
    versionLatestPrefix: string;
    updateCommandLabel: string;
    updateCommandCopied: string;
  };
  onboarding: {
    stepCounter: string;
    steps: {
      how: string;
      identity: string;
      folder: string;
      brain: string;
      ready: string;
    };
    nav: {
      back: string;
      next: string;
      skip: string;
      skipAll: string;
      close: string;
      copy: string;
      copied: string;
      startTour: string;
      finishNoTour: string;
    };
    status: {
      saving: string;
      saved: string;
      error: string;
      scanning: string;
      scanError: string;
      setupLoading: string;
      demoSeeding: string;
      demoError: string;
    };
    how: {
      title: string;
      subtitle: string;
      localNote: string;
      cards: {
        gate: { title: string; body: string };
        agents: { title: string; body: string };
        brain: { title: string; body: string };
        audit: { title: string; body: string };
      };
    };
    identity: {
      title: string;
      subtitle: string;
      demoNoticeTitle: string;
      demoNoticeBody: string;
      operatorLabel: string;
      operatorPlaceholder: string;
      companyLabel: string;
      companyPlaceholder: string;
    };
    folder: {
      title: string;
      subtitle: string;
      rootLabel: string;
      rootPlaceholder: string;
      exclusionsLabel: string;
      exclusionsPlaceholder: string;
      scan: string;
      rescan: string;
      found: string;
      selected: string;
      confirm: string;
      confirmed: string;
      code: string;
      noCode: string;
      promptTitle: string;
      promptHint: string;
      promptEmpty: string;
      promptReadSetup: string;
      promptRoot: string;
      promptSources: string;
      promptExclusions: string;
      promptDerive: string;
    };
    brain: {
      title: string;
      subtitle: string;
      sourcesTitle: string;
      docs: string;
      docsHint: string;
      repos: string;
      reposHint: string;
      email: string;
      kb: string;
      enterpriseSoon: string;
      cycleLabel: string;
      cycleHint: string;
    };
    ready: {
      title: string;
      subtitle: string;
      recapTitle: string;
      folders: string;
      sources: string;
      cycle: string;
      setupPreview: string;
      initTitle: string;
      initBody: string;
      demoTitle: string;
      demoBody: string;
    };
  };
  tour: {
    label: string;
    close: string;
    next: string;
    back: string;
    clickToContinue: string;
    skip: string;
    finalEyebrow: string;
    clearDemo: string;
    rerun: string;
    keepDemo: string;
    clearingDemo: string;
    clearError: string;
    steps: Record<string, { title: string; body: string }>;
  };
  emptyStates: {
    todos: {
      title: string;
      body: string;
      action: string;
    };
    universe: {
      title: string;
      body: string;
      action: string;
    };
  };
  graph: {
    loading: string;
    emptyTitle: string;
    emptyBody: string;
    emptyAction: string;
    lenses: {
      cosmoDesc: string;
      codexDesc: string;
    };
  };
  taskSurface: {
    title: string;
    subtitle: string;
    loading: string;
    error: string;
    empty: string;
    retry: string;
    views: {
      kanban: string;
      project: string;
      calendar: string;
    };
    filters: {
      all: string;
      search: string;
      searchPlaceholder: string;
      searchPanel: string;
      projectResults: string;
      program: string;
      scope: string;
      type: string;
      ownerAll: string;
      ownerAgent: string;
      ownerHuman: string;
      work: string;
      personal: string;
      code: string;
      noCode: string;
      remove: string;
    };
    columns: {
      approved: string;
      in_progress: string;
      review: string;
      completed: string;
      rejected: string;
    };
    card: {
      due: string;
      noDue: string;
      human: string;
      agent: string;
      ease: string;
      impact: string;
      pr: string;
    };
    drawer: {
      descriptionTitle: string;
      warningPrefix: string;
      dueTitle: string;
      postpone: string;
      newDate: string;
      confirm: string;
      cancel: string;
      iceTitle: string;
      prTitle: string;
      diffLink: string;
      dependencies: string;
      blockedBy: string;
      notes: string;
      notesHelper: string;
      notePlaceholder: string;
      addNote: string;
      timeline: string;
      created: string;
      updated: string;
      comments: string;
      noDescription: string;
      feedbackPlaceholder: string;
    };
    actions: {
      start: string;
      send_review: string;
      complete: string;
      approve: string;
      return: string;
      reopen: string;
    };
    followUp: {
      title: string;
      body: string;
      field: string;
      create: string;
      skip: string;
      prefix: string;
    };
  };
  todos: {
    title: string;
    subtitle: string;
    capturePlaceholder: string;
    captureSubmit: string;
    captureSaving: string;
    loading: string;
    error: string;
    retry: string;
    empty: string;
    personal: string;
    assign: string;
    assignProject: string;
    save: string;
    cancel: string;
    originBadge: string;
    doerHuman: string;
    doerAgent: string;
    gateHint: string;
    filters: {
      all: string;
      decisions: string;
      promemoria: string;
      idea: string;
      brain: string;
    };
    horizons: {
      overdue: string;
      today: string;
      tomorrow: string;
      week: string;
      later: string;
    };
    types: {
      promemoria: string;
      azione: string;
      idea: string;
      decidi: string;
      approva: string;
      rivedi: string;
    };
    sources: {
      user: string;
      agent: string;
      brain: string;
    };
    actions: {
      complete: string;
      discard: string;
      postpone: string;
      delegate: string;
      promote: string;
      confirm: string;
      revise: string;
      approve: string;
      reject: string;
      review: string;
      feedback: string;
      sendFeedback: string;
    };
    captions: {
      captured: string;
      completed: string;
      discarded: string;
      postponed: string;
      delegated: string;
      promoted: string;
      decided: string;
      revise: string;
      approved: string;
      rejected: string;
      reviewing: string;
      feedback: string;
      reassigned: string;
      operatorSession: string;
      actionFailed: string;
    };
    drawer: {
      close: string;
      metaTitle: string;
      contextTitle: string;
      payloadTitle: string;
      taskReview: string;
      finding: string;
      memoryOperation: string;
      branch: string;
      status: string;
      summary: string;
      evidence: string;
      proposedWrite: string;
      noPayload: string;
      feedbackPlaceholder: string;
    };
  };
  projects: {
    navigator: {
      search: string;
      loading: string;
      empty: string;
      count: string;
      collapse: string;
      expand: string;
    };
    overview: {
      title: string;
      subtitle: string;
      openTasks: string;
      progressTooltip: string;
      empty: string;
    };
    dashboard: {
      breadcrumbRoot: string;
      loading: string;
      error: string;
      back: string;
      color: string;
      colorSaved: string;
      colorReset: string;
      colorError: string;
      resetColor: string;
      headerTaskButton: string;
      code: string;
      noCode: string;
      personal: string;
      work: string;
      status: string;
      statusCaption: string;
      pulseOpen: string;
      pulseReview: string;
      pulseProgress: string;
      pulseProgressTooltip: string;
      diary: string;
      diarySource: string;
      diaryEmpty: string;
      decisions: string;
      decisionsPath: string;
      current: string;
      superseded: string;
      decisionsEmpty: string;
      questions: string;
      questionsEmpty: string;
      addTodo: string;
      addedTodo: string;
      todoError: string;
      brain: string;
      brainSource: string;
      brainEmpty: string;
      docs: string;
      docsCodeCaption: string;
      viewAllDocs: string;
      docsEmpty: string;
      relations: string;
      relationRelated: string;
      relationDepends: string;
      addRelation: string;
      removeRelation: string;
      relationSaved: string;
      relationRemoved: string;
      relationError: string;
      more: string;
      collapse: string;
      codePanel: string;
      codeBanner: string;
      branch: string;
      pullRequests: string;
      noPullRequests: string;
    };
    docsModal: {
      title: string;
      search: string;
      all: string;
      open: string;
      empty: string;
    };
    editor: {
      preview: string;
      edit: string;
      save: string;
      saving: string;
      saved: string;
      loadError: string;
      saveError: string;
      unsupported: string;
      close: string;
    };
  };
  diario: {
    loading: string;
    pastBanner: string;
    todayFallback: string;
    projectFallback: string;
    sections: {
      narrative: string;
      decisions: string;
      progress: string;
      context: string;
    };
    narrative: {
      fallbackCaption: string;
      emptyFallback: string;
    };
    snapshot: {
      projectsTouched: string;
      noProjects: string;
      decisions: string;
      progress: string;
      context: string;
    };
    actions: {
      addTodo: string;
      delegate: string;
      adding: string;
      delegating: string;
      added: string;
      delegated: string;
      error: string;
    };
    progress: {
      changePrefix: string;
      decisionPrefix: string;
    };
    timeline: {
      label: string;
      today: string;
    };
    limit: {
      quietTitle: string;
      quietBody: string;
      notRunTitle: string;
      notRunBody: string;
      openLatest: string;
      runNow: string;
      runningNow: string;
      polling: string;
      alreadyRunning: string;
      runError: string;
      cycleCompleted: string;
      openCycleDay: string;
      runTimeout: string;
    };
    error: {
      title: string;
      body: string;
    };
  };
}
