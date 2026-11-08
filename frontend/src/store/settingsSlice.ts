import { createSlice, PayloadAction, createAsyncThunk } from '@reduxjs/toolkit';

export interface SettingsState {
    agents: any[];
    mcpServers: any[];
    customTools: any[];
    models: { local: string[], cloud: string[], providers: Record<string, any>, reasoning_capable?: string[], reasoning_without_off?: string[], reasoning_off_minimum?: string[] };
    loading: boolean;
    initialized: boolean;
}

const initialState: SettingsState = {
    agents: [],
    mcpServers: [],
    customTools: [],
    models: { local: [], cloud: [], providers: {} },
    loading: false,
    initialized: false,
};

export const fetchAllSettingsData = createAsyncThunk(
    'settings/fetchAll',
    async () => {
        const [agentsRes, mcpRes, toolsRes, modelsRes] = await Promise.all([
            fetch('/api/agents'),
            fetch('/api/mcp/servers'),
            fetch('/api/tools/custom'),
            fetch('/api/models')
        ]);
        
        return {
            agents: agentsRes.ok ? await agentsRes.json() : [],
            mcpServers: mcpRes.ok ? await mcpRes.json() : [],
            customTools: toolsRes.ok ? await toolsRes.json() : [],
            models: modelsRes.ok ? await modelsRes.json() : { local: [], cloud: [], providers: {} },
        };
    }
);

export const settingsSlice = createSlice({
    name: 'settings',
    initialState,
    reducers: {
        setAgents: (state, action: PayloadAction<any[]>) => { state.agents = action.payload; },
        addAgent: (state, action: PayloadAction<any>) => { state.agents.push(action.payload); },
        updateAgent: (state, action: PayloadAction<any>) => { 
            const index = state.agents.findIndex(a => a.id === action.payload.id);
            if (index !== -1) state.agents[index] = action.payload;
        },
        removeAgent: (state, action: PayloadAction<string>) => {
            state.agents = state.agents.filter(a => a.id !== action.payload);
        },
        
        setMcpServers: (state, action: PayloadAction<any[]>) => { state.mcpServers = action.payload; },
        addMcpServer: (state, action: PayloadAction<any>) => { state.mcpServers.push(action.payload); },
        updateMcpServerStatus: (state, action: PayloadAction<{ name: string; status: string }>) => {
            const server = state.mcpServers.find(s => s.name === action.payload.name);
            if (server) server.status = action.payload.status;
        },
        removeMcpServer: (state, action: PayloadAction<string>) => {
            state.mcpServers = state.mcpServers.filter(s => s.name !== action.payload);
        },
        
        setCustomTools: (state, action: PayloadAction<any[]>) => { state.customTools = action.payload; },
        addCustomTool: (state, action: PayloadAction<any>) => { state.customTools.push(action.payload); },
        updateCustomTool: (state, action: PayloadAction<any>) => {
            const index = state.customTools.findIndex(t => t.name === action.payload.name);
            if (index !== -1) state.customTools[index] = action.payload;
            else state.customTools.push(action.payload);
        },
        removeCustomTool: (state, action: PayloadAction<string>) => {
            state.customTools = state.customTools.filter(t => t.name !== action.payload);
        },
    },
    extraReducers: (builder) => {
        builder.addCase(fetchAllSettingsData.pending, (state) => {
            state.loading = true;
        });
        builder.addCase(fetchAllSettingsData.fulfilled, (state, action) => {
            state.loading = false;
            state.initialized = true;
            state.agents = Array.isArray(action.payload.agents) ? action.payload.agents : [];
            state.mcpServers = Array.isArray(action.payload.mcpServers) ? action.payload.mcpServers : [];
            state.customTools = Array.isArray(action.payload.customTools) ? action.payload.customTools : [];
            state.models = action.payload.models;
        });
        builder.addCase(fetchAllSettingsData.rejected, (state) => {
            state.loading = false;
        });
    }
});

export const { 
    setAgents, addAgent, updateAgent, removeAgent, 
    setMcpServers, addMcpServer, updateMcpServerStatus, removeMcpServer, 
    setCustomTools, addCustomTool, updateCustomTool, removeCustomTool 
} = settingsSlice.actions;

export default settingsSlice.reducer;
